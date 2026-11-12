"""Unit tests for adapters: golden argv, profile flags, model parsing, error mapping, and fallbacks.

ACC: N6, N7, N8, N15, N16, G24
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

import mesh.constants as c
from mesh.adapters.base import (
    CommandSpec,
    EffortNotAllowed,
    EffortUnsupported,
    EXPECTED_HELP_HASHES,
    ProfileUnsupported,
    PromptTooLong,
    RunRequest,
    find_missing_flags,
    is_flag_present,
    normalize_help_text,
    parse_version_output,
    run_readonly_cmd,
)
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.agy import AgyAdapter
from mesh.adapters.kiro import KiroAdapter
from mesh.adapters.vibe import VibeAdapter
from mesh.adapters.hermes import HermesAdapter
from mesh.adapters.openclaw import OpenclawAdapter
from mesh.adapters.none_adapter import NoneAdapter
from mesh.adapters.fake import FakeAdapter
from mesh.adapters.registry import get_adapter, list_adapters
from tests.stubs import (
    create_failing_stub,
    create_garbage_stub,
    create_hanging_stub,
    create_missing_flag_stub,
    create_modified_help_stub,
    create_stale_stub,
    create_stderr_help_stub,
    create_test_stubs,
)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES_ROOT = os.path.join(REPO_ROOT, "tests", "fixtures", "real")


class TestAdapters(unittest.TestCase):
    """Test suite covering adapter specifications and golden fixtures."""

    @classmethod
    def setUpClass(cls):
        cls.stubs_dir = tempfile.mkdtemp(prefix="unit-stubs-")
        create_test_stubs(cls.stubs_dir)
        cls.orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{cls.stubs_dir}:{cls.orig_path}"

    @classmethod
    def tearDownClass(cls):
        os.environ["PATH"] = cls.orig_path
        shutil.rmtree(cls.stubs_dir, ignore_errors=True)

    def setUp(self):
        self.droid = DroidAdapter()
        self.agy = AgyAdapter()
        self.kiro = KiroAdapter()
        self.vibe = VibeAdapter()
        self.fake = FakeAdapter()

    def test_adapters_build_droid(self):
        """ACC: N6 - Droid golden argv per profile and resume."""
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_file = os.path.join(tmpdir, "prompt.md")
            with open(prompt_file, "w") as f:
                f.write("Task instructions")

            # 1. Readonly profile
            req_ro = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-opus-5-5",
                effort="high",
                profile="readonly",
                options={"run_id": "run_001"},
            )
            spec_ro = self.droid.build(req_ro)
            expected_ro = [
                "droid", "exec", "--output-format", "stream-json", "--cwd", "/ws/task1",
                "--model", "claude-opus-5-5", "--reasoning-effort", "high",
                "--log-group-id", "run_001", "--file", prompt_file,
            ]
            self.assertEqual(spec_ro.argv, expected_ro)
            self.assertEqual(spec_ro.stdin, "devnull")

            # 2. Edit profile
            req_edit = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-opus-5-5",
                effort=None,
                profile="edit",
                options={"run_id": "run_002"},
            )
            spec_edit = self.droid.build(req_edit)
            self.assertIn("--auto", spec_edit.argv)
            self.assertIn("low", spec_edit.argv)

            # 3. Dev profile
            req_dev = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-opus-5-5",
                effort=None,
                profile="dev",
                options={"run_id": "run_003"},
            )
            spec_dev = self.droid.build(req_dev)
            self.assertIn("--auto", spec_dev.argv)
            self.assertIn("medium", spec_dev.argv)

            # 4. Elevated profile
            req_elev = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-opus-5-5",
                effort=None,
                profile="elevated",
                options={"run_id": "run_004"},
            )
            spec_elev = self.droid.build(req_elev)
            self.assertIn("--auto", spec_elev.argv)
            self.assertIn("high", spec_elev.argv)

            # 5. Unrestricted profile (trust-all)
            req_unres = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-opus-5-5",
                effort=None,
                profile="unrestricted",
                options={"run_id": "run_005"},
            )
            spec_unres = self.droid.build(req_unres)
            self.assertIn("--skip-permissions-unsafe", spec_unres.argv)
            self.assertNotIn("--auto", spec_unres.argv)

            # 6. Resume session
            req_resume = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-opus-5-5",
                effort=None,
                profile="readonly",
                resume_session_id="sess_abc123",
                options={"run_id": "run_006"},
            )
            spec_resume = self.droid.build(req_resume)
            self.assertIn("--session-id", spec_resume.argv)
            self.assertIn("sess_abc123", spec_resume.argv)

            # Forbidden flags check: no -w, no --worktree, no -c, no --continue
            for spec in [spec_ro, spec_edit, spec_dev, spec_elev, spec_unres, spec_resume]:
                self.assertNotIn("-w", spec.argv)
                self.assertNotIn("--worktree", spec.argv)
                self.assertNotIn("-c", spec.argv)
                self.assertNotIn("--continue", spec.argv)
                self.assertNotIn("--resume", spec.argv)

    def test_adapters_build_agy(self):
        """ACC: N6 - Agy golden argv per profile and resume."""
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_file = os.path.join(tmpdir, "prompt.md")
            with open(prompt_file, "w") as f:
                f.write("Task content")

            # 1. Readonly
            req_ro = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-sonnet-4-6",
                effort="medium",
                profile="readonly",
                timeout_s=300,
            )
            spec_ro = self.agy.build(req_ro)
            expected_ro = [
                "agy", "--print", "--output-format", "stream-json", "--model", "claude-sonnet-4-6",
                "--effort", "medium", "--mode", "plan", "--print-timeout", "300s", "Task content",
            ]
            self.assertEqual(spec_ro.argv, expected_ro)

            # 2. Edit
            req_edit = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-sonnet-4-6",
                effort=None,
                profile="edit",
                timeout_s=300,
            )
            spec_edit = self.agy.build(req_edit)
            self.assertIn("--mode", spec_edit.argv)
            self.assertIn("accept-edits", spec_edit.argv)

            # 3. Dev and Elevated are unsupported in M1
            for p in ["dev", "elevated"]:
                req = RunRequest(
                    prompt_path=prompt_file,
                    workspace="/ws/task1",
                    model="claude-sonnet-4-6",
                    effort=None,
                    profile=p,
                )
                with self.assertRaises(ProfileUnsupported):
                    self.agy.build(req)

            # 4. Unrestricted (trust-all)
            req_unres = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-sonnet-4-6",
                effort=None,
                profile="unrestricted",
                timeout_s=300,
            )
            spec_unres = self.agy.build(req_unres)
            self.assertIn("--dangerously-skip-permissions", spec_unres.argv)
            self.assertNotIn("--mode", spec_unres.argv)

            # 5. Resume
            req_resume = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-sonnet-4-6",
                effort=None,
                profile="readonly",
                resume_session_id="conv_xyz789",
                timeout_s=300,
            )
            spec_resume = self.agy.build(req_resume)
            self.assertIn("--conversation", spec_resume.argv)
            self.assertIn("conv_xyz789", spec_resume.argv)

            # Forbidden flags
            for spec in [spec_ro, spec_edit, spec_unres, spec_resume]:
                self.assertNotIn("-c", spec.argv)
                self.assertNotIn("--continue", spec.argv)
                self.assertNotIn("--resume", spec.argv)

    def test_adapters_build_kiro(self):
        """ACC: N6 - Kiro golden argv per profile and resume."""
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_file = os.path.join(tmpdir, "prompt.md")
            with open(prompt_file, "w") as f:
                f.write("Kiro prompt instructions")

            # 1. Readonly
            req_ro = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-opus-5.5",
                effort="high",
                profile="readonly",
            )
            spec_ro = self.kiro.build(req_ro)
            expected_ro = [
                "kiro-cli", "chat", "--no-interactive", "--output-format", "stream-json",
                "--model", "claude-opus-5.5", "--effort", "high",
                "--trust-tools=", "Kiro prompt instructions",
            ]
            self.assertEqual(spec_ro.argv, expected_ro)

            # 2. Edit
            req_edit = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-opus-5.5",
                effort=None,
                profile="edit",
            )
            spec_edit = self.kiro.build(req_edit)
            self.assertIn("--trust-tools=fs_read,fs_write", spec_edit.argv)

            # 3. Dev and Elevated unsupported
            for p in ["dev", "elevated"]:
                req = RunRequest(
                    prompt_path=prompt_file,
                    workspace="/ws/task1",
                    model="claude-opus-5.5",
                    effort=None,
                    profile=p,
                )
                with self.assertRaises(ProfileUnsupported):
                    self.kiro.build(req)

            # 4. Unrestricted (trust-all)
            req_unres = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-opus-5.5",
                effort=None,
                profile="unrestricted",
            )
            spec_unres = self.kiro.build(req_unres)
            self.assertIn("--trust-all-tools", spec_unres.argv)
            self.assertNotIn("--trust-tools=", spec_unres.argv)

            # 5. Resume
            req_resume = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="claude-opus-5.5",
                effort=None,
                profile="readonly",
                resume_session_id="kiro_sess_456",
            )
            spec_resume = self.kiro.build(req_resume)
            self.assertIn("--resume-id", spec_resume.argv)
            self.assertIn("kiro_sess_456", spec_resume.argv)

            # Forbidden flags: no bare --resume or -r
            for spec in [spec_ro, spec_edit, spec_unres, spec_resume]:
                self.assertNotIn("-r", spec.argv)
                self.assertNotIn("--resume", spec.argv)
                self.assertNotIn("-c", spec.argv)
                self.assertNotIn("--continue", spec.argv)

    def test_adapters_build_vibe(self):
        """ACC: N6 - Vibe golden argv per profile and resume."""
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_file = os.path.join(tmpdir, "prompt.md")
            with open(prompt_file, "w") as f:
                f.write("Vibe prompt instructions")

            # 1. Readonly
            req_ro = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="mistral-large",
                effort=None,
                profile="readonly",
                timeout_s=600,
            )
            spec_ro = self.vibe.build(req_ro)
            out_dir = os.path.join(tmpdir, "vibe-out")
            expected_ro = [
                "vibe", "--prompt-file", prompt_file, "--output", "streaming",
                "--workdir", "/ws/task1", "--model", "mistral-large",
                "--agent", "ask", "--time-limit", "600", "--output-dir", out_dir,
            ]
            self.assertEqual(spec_ro.argv, expected_ro)

            # 2. Edit
            req_edit = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="mistral-large",
                effort=None,
                profile="edit",
                timeout_s=600,
            )
            spec_edit = self.vibe.build(req_edit)
            self.assertIn("--agent", spec_edit.argv)
            self.assertIn("accept-edits", spec_edit.argv)

            # 3. Dev
            req_dev = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="mistral-large",
                effort=None,
                profile="dev",
                timeout_s=600,
            )
            spec_dev = self.vibe.build(req_dev)
            self.assertIn("--agent", spec_dev.argv)
            self.assertIn("smart-approve", spec_dev.argv)

            # 4. Elevated is unsupported
            req_elev = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="mistral-large",
                effort=None,
                profile="elevated",
                timeout_s=600,
            )
            with self.assertRaises(ProfileUnsupported):
                self.vibe.build(req_elev)

            # 5. Unrestricted (trust-all)
            req_unres = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="mistral-large",
                effort=None,
                profile="unrestricted",
                timeout_s=600,
            )
            spec_unres = self.vibe.build(req_unres)
            self.assertIn("--agent", spec_unres.argv)
            self.assertIn("auto-approve", spec_unres.argv)
            self.assertIn("--auto-approve", spec_unres.argv)

            # 6. Resume: model is omitted with resume
            req_resume = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="mistral-large",
                effort=None,
                profile="readonly",
                resume_session_id="vibe_sess_999",
                timeout_s=600,
            )
            spec_resume = self.vibe.build(req_resume)
            self.assertIn("--resume", spec_resume.argv)
            self.assertIn("vibe_sess_999", spec_resume.argv)
            self.assertNotIn("--model", spec_resume.argv)

            # 7. Effort on vibe raises EffortUnsupported
            req_effort = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task1",
                model="mistral-large",
                effort="high",
                profile="readonly",
            )
            with self.assertRaises(EffortUnsupported):
                self.vibe.build(req_effort)

            # Forbidden flags
            for spec in [spec_ro, spec_edit, spec_dev, spec_unres, spec_resume]:
                self.assertNotIn("-c", spec.argv)
                self.assertNotIn("--continue", spec.argv)
                self.assertNotIn("--worktree", spec.argv)

    def test_adapters_models_parse(self):
        """ACC: N6, N7 - Model parsing reads real committed fixtures and produces exact model lists."""
        # 1. Droid: reads tests/fixtures/real/droid/1.3.0/exec-help.txt
        droid_models = self.droid.list_models()
        self.assertIsNotNone(droid_models)
        self.assertIn("claude-opus-5-5", droid_models)
        self.assertIn("gemini-3.8-flash", droid_models)
        self.assertIn("gpt-6.1-sol", droid_models)
        self.assertIn("glm-5.3", droid_models)

        # Directly verify parse_models_from_help with fixture file
        with open(os.path.join(FIXTURES_ROOT, "droid", "1.3.0", "exec-help.txt"), "r", encoding="utf-8") as f:
            direct_droid = DroidAdapter.parse_models_from_help(f.read())
        self.assertEqual(droid_models, direct_droid)

        # 2. Agy: reads tests/fixtures/real/agy/1.5.2/models.txt
        agy_models = self.agy.list_models()
        self.assertIsNotNone(agy_models)
        self.assertIn("gemini-3.8-flash-high", agy_models)
        self.assertIn("claude-sonnet-4-6", agy_models)
        self.assertIn("claude-opus-4-6-thinking", agy_models)
        self.assertIn("gpt-oss-120b-medium", agy_models)

        # Directly verify parse_models_from_output with fixture file
        with open(os.path.join(FIXTURES_ROOT, "agy", "1.5.2", "models.txt"), "r", encoding="utf-8") as f:
            direct_agy = AgyAdapter.parse_models_from_output(f.read())
        self.assertEqual(agy_models, direct_agy)

        # 3. Kiro: reads tests/fixtures/real/kiro/3.1.0/list-models.json
        kiro_models = self.kiro.list_models()
        self.assertIsNotNone(kiro_models)
        self.assertEqual(len(kiro_models), 22)
        self.assertIn("claude-opus-5.5", kiro_models)
        self.assertIn("gpt-5.6-sol", kiro_models)
        self.assertIn("qwen3-coder-next", kiro_models)

        # Directly verify parse_models_from_json with fixture file
        with open(os.path.join(FIXTURES_ROOT, "kiro", "3.1.0", "list-models.json"), "r", encoding="utf-8") as f:
            direct_kiro = KiroAdapter.parse_models_from_json(json.load(f))
        self.assertEqual(kiro_models, direct_kiro)

        # 4. Vibe: returns None (no list command per §14.6 and ACC: N7)
        vibe_models = self.vibe.list_models()
        self.assertIsNone(vibe_models)

    def test_adapters_parse_errors(self):
        """ACC: N6 - Maps kiro 'failed to set effort' stderr to model_or_effort_rejected."""
        stderr_sample = "failed to set effort ... model does not support additional fields"
        err = KiroAdapter.parse_stderr_error(stderr_sample)
        self.assertIsNotNone(err)
        self.assertEqual(err["reason"], "model_or_effort_rejected")
        self.assertIn("README observation", err["details"]["note"])
        self.assertEqual(err["details"]["stderr"], stderr_sample)

    def test_adapter_flags_in_fixtures(self):
        """ACC: N15 - Every flag in every golden argv appears in that adapter's help fixture and template allow-list."""
        # Check Droid help fixture
        droid_help_path = os.path.join(FIXTURES_ROOT, "droid", "1.3.0", "exec-help.txt")
        with open(droid_help_path, "r", encoding="utf-8") as f:
            droid_help = f.read()

        droid_probe = self.droid.probe()
        for flag in droid_probe.flags:
            self.assertIn(flag, droid_help)

        # Check Agy help fixture
        agy_help_path = os.path.join(FIXTURES_ROOT, "agy", "1.5.2", "help.txt")
        with open(agy_help_path, "r", encoding="utf-8") as f:
            agy_help = f.read()

        agy_probe = self.agy.probe()
        for flag in agy_probe.flags:
            self.assertIn(flag, agy_help)

        # Check Kiro chat-help fixture
        kiro_help_path = os.path.join(FIXTURES_ROOT, "kiro", "3.1.0", "chat-help.txt")
        with open(kiro_help_path, "r", encoding="utf-8") as f:
            kiro_help = f.read()

        kiro_probe = self.kiro.probe()
        for flag in kiro_probe.flags:
            base_flag = flag.split("=")[0]
            self.assertIn(base_flag, kiro_help)

        # Check Vibe help fixture
        vibe_help_path = os.path.join(FIXTURES_ROOT, "vibe", "3.1.0", "help.txt")
        with open(vibe_help_path, "r", encoding="utf-8") as f:
            vibe_help = f.read()

        vibe_probe = self.vibe.probe()
        for flag in vibe_probe.flags:
            self.assertIn(flag, vibe_help)

        # Vibe --trust appears ONLY when trust_workdir is True
        with tempfile.TemporaryDirectory() as tmpdir:
            pfile = os.path.join(tmpdir, "p.md")
            with open(pfile, "w") as f:
                f.write("p")
            req_no_trust = RunRequest(prompt_path=pfile, workspace="/ws", model="m", effort=None, profile="readonly")
            spec_no_trust = self.vibe.build(req_no_trust)
            self.assertNotIn("--trust", spec_no_trust.argv)

            req_trust = RunRequest(prompt_path=pfile, workspace="/ws", model="m", effort=None, profile="readonly", options={"trust_workdir": True})
            spec_trust = self.vibe.build(req_trust)
            self.assertIn("--trust", spec_trust.argv)

    def test_prompt_fallback(self):
        """ACC: N16 - Prompt fallback behavior for 150,000-byte prompts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_file = os.path.join(tmpdir, "prompt.md")
            large_prompt = "A" * 150_000
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(large_prompt)

            # 1. Droid: uses --file flag regardless of size
            req_droid = RunRequest(prompt_path=prompt_file, workspace="/ws", model="claude-opus-5-5", effort=None, profile="readonly")
            spec_droid = self.droid.build(req_droid)
            self.assertIn("--file", spec_droid.argv)
            self.assertIn(prompt_file, spec_droid.argv)

            # 2. Vibe: uses --prompt-file flag regardless of size
            req_vibe = RunRequest(prompt_path=prompt_file, workspace="/ws", model="m", effort=None, profile="readonly")
            spec_vibe = self.vibe.build(req_vibe)
            self.assertIn("--prompt-file", spec_vibe.argv)
            self.assertIn(prompt_file, spec_vibe.argv)

            # 3. Agy: replaces argv with pointer sentence and adds --add-dir <run_dir>
            req_agy = RunRequest(prompt_path=prompt_file, workspace="/ws", model="claude-sonnet-4-6", effort=None, profile="readonly")
            spec_agy = self.agy.build(req_agy)
            self.assertIn("--add-dir", spec_agy.argv)
            self.assertIn(tmpdir, spec_agy.argv)
            expected_sentence = f"Read the file {prompt_file} and follow the instructions in it exactly. It is your complete task."
            self.assertEqual(spec_agy.argv[-1], expected_sentence)

            # 4. Kiro edit: replaces argv with pointer sentence
            req_kiro_edit = RunRequest(prompt_path=prompt_file, workspace="/ws", model="claude-opus-5.5", effort=None, profile="edit")
            spec_kiro_edit = self.kiro.build(req_kiro_edit)
            self.assertEqual(spec_kiro_edit.argv[-1], expected_sentence)

            # 5. Kiro readonly: raises PROMPT_TOO_LONG (ACC: N16)
            req_kiro_ro = RunRequest(prompt_path=prompt_file, workspace="/ws", model="claude-opus-5.5", effort=None, profile="readonly")
            with self.assertRaises(PromptTooLong) as ctx:
                self.kiro.build(req_kiro_ro)
            self.assertEqual(ctx.exception.code, c.PROMPT_TOO_LONG)
            self.assertEqual(ctx.exception.details["bytes"], 150_000)
            self.assertEqual(ctx.exception.details["limit"], 100_000)
            self.assertEqual(ctx.exception.details["adapter"], "kiro")
            self.assertEqual(ctx.exception.details["profile"], "readonly")

    def test_prompt_t5_too_long(self):
        """ACC: N16 - Kiro readonly task whose spec fits but rework feedback pushes over limit raises PROMPT_TOO_LONG."""
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_file = os.path.join(tmpdir, "prompt.md")
            # 80,000 bytes spec + 30,000 bytes rework feedback = 110,000 bytes (> 100,000 limit)
            spec_and_rework = "Spec content\n" * 5000 + "Rework feedback\n" * 3000
            prompt_bytes = len(spec_and_rework.encode("utf-8"))
            self.assertGreater(prompt_bytes, 100_000)
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(spec_and_rework)

            req = RunRequest(
                prompt_path=prompt_file,
                workspace="/ws/task_t5",
                model="claude-opus-5.5",
                effort=None,
                profile="readonly",
            )
            with self.assertRaises(PromptTooLong) as ctx:
                self.kiro.build(req)
            self.assertEqual(ctx.exception.code, c.PROMPT_TOO_LONG)
            self.assertEqual(ctx.exception.details["bytes"], prompt_bytes)
            self.assertEqual(ctx.exception.details["limit"], 100_000)
            self.assertEqual(ctx.exception.details["adapter"], "kiro")
            self.assertEqual(ctx.exception.details["profile"], "readonly")

    def test_profile_flags_and_separation(self):
        """ACC: G24 - Profile flag uniqueness, trust-all restriction, and profile/effort separation."""
        adapters = [self.droid, self.agy, self.kiro, self.vibe, self.fake]
        trust_all_flags = {
            "--skip-permissions-unsafe",
            "--dangerously-skip-permissions",
            "--trust-all-tools",
            "--auto-approve",
        }

        for adapter in adapters:
            seen_flag_sets = set()
            for profile in ["readonly", "edit", "dev", "elevated", "unrestricted"]:
                try:
                    flags = adapter.profile_flags(profile)
                    flag_tuple = tuple(flags)
                    self.assertNotIn(flag_tuple, seen_flag_sets, f"Duplicate profile flags for {adapter.id} profile {profile}")
                    seen_flag_sets.add(flag_tuple)

                    # Trust-all check
                    for flag in flags:
                        if flag in trust_all_flags:
                            self.assertEqual(
                                profile,
                                "unrestricted",
                                f"Trust-all flag {flag} appeared in profile {profile} on {adapter.id} (allowed only in unrestricted)",
                            )
                except ProfileUnsupported:
                    # Expected for unsupported profiles
                    if adapter.id == "agy":
                        self.assertIn(profile, ("dev", "elevated"))
                    elif adapter.id == "kiro":
                        self.assertIn(profile, ("dev", "elevated"))
                    elif adapter.id == "vibe":
                        self.assertEqual(profile, "elevated")
                    elif adapter.id in ("droid", "fake"):
                        self.fail(f"{adapter.id} unexpectedly raised ProfileUnsupported for {profile}")

        # Effort and profile separation
        with tempfile.TemporaryDirectory() as tmpdir:
            pfile = os.path.join(tmpdir, "p.md")
            with open(pfile, "w") as f:
                f.write("p")

            # Agy model encoding effort check (ACC: D17)
            req_agy_conflict = RunRequest(
                prompt_path=pfile,
                workspace="/ws",
                model="gemini-3.8-flash-high",
                effort="high",
                profile="readonly",
            )
            with self.assertRaises(EffortNotAllowed) as ctx:
                self.agy.build(req_agy_conflict)
            self.assertEqual(ctx.exception.details["reason"], "model_encodes_effort")

    def test_adapter_stale_version(self):
        """ACC: N8 - Probe returns stale verification if probed version differs from verification version."""
        probe = self.droid.probe(probed_version="99.0.0")
        for cname, cinfo in probe.capabilities.items():
            if cinfo["verification"] != "none":
                self.assertEqual(cinfo["verification"], "stale")

    def test_expected_help_hashes_constants(self):
        """Verify embedded expected help hashes match plan §14.11 constants without importing tests."""
        self.assertIn("droid", EXPECTED_HELP_HASHES)
        self.assertEqual(EXPECTED_HELP_HASHES["droid"], "1df2e88e997f1488d22fa59340bc79c13198a143363981ee8825a641260143f3")
        self.assertEqual(EXPECTED_HELP_HASHES["droid_exec"], "61880b6d4c40e86e497653e95f01beb3a7cdad223a4fd98010d53d6dce30449a")
        self.assertEqual(EXPECTED_HELP_HASHES["agy"], "e92946f85cec63eca61f93014ca49f53b1e84277eac7954e7fac7d8d712940fd")
        self.assertEqual(EXPECTED_HELP_HASHES["kiro"], "353d58ab2ccd44f36372cf16a00a3d051519ccaf4778b5269d6dded4622d17d5")
        self.assertEqual(EXPECTED_HELP_HASHES["kiro_chat"], "56bf95560094d7bee17372d18b8fc40ff834ff6b72c338863f91428719b00eeb")
        self.assertEqual(EXPECTED_HELP_HASHES["vibe"], "15d858a3434d0664ee8af445933ef438bc7a6efac9961de31351013c0d2ee158")

    def test_parse_version_output(self):
        """Verify version parser handles standard CLI version outputs."""
        self.assertEqual(parse_version_output("1.5.2\n"), "1.5.2")
        self.assertEqual(parse_version_output("agy version 1.5.2\n"), "1.5.2")
        self.assertEqual(parse_version_output("droid 1.3.0\n"), "1.3.0")
        self.assertEqual(parse_version_output("kiro-cli 3.1.0\n"), "3.1.0")
        self.assertEqual(parse_version_output("vibe 3.1.0 (rev 1234)\n"), "3.1.0")
        self.assertEqual(parse_version_output("v2.0.1-rc1"), "2.0.1-rc1")
        self.assertIsNone(parse_version_output("no semver here"))
        self.assertIsNone(parse_version_output(""))

    def test_probe_error_and_unverified(self):
        """Verify probe status error on binary failure and unverified on garbage output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Failing stub
            fail_bin = create_failing_stub(tmpdir, "failing_bin", exit_code=1)
            probe_fail = self.droid.probe(binary_path=fail_bin)
            self.assertEqual(probe_fail.status, "error")
            self.assertIsNone(probe_fail.version)
            self.assertIn("error", probe_fail.details)

            # Garbage stub
            garbage_bin = create_garbage_stub(tmpdir, "garbage_bin", text="UNPARSEABLE_JUNK")
            probe_garbage = self.droid.probe(binary_path=garbage_bin)
            self.assertEqual(probe_garbage.status, "unverified")
            self.assertIsNone(probe_garbage.version)
            self.assertIn("unparseable_version", probe_garbage.details["error"])

    def test_probe_timeout(self):
        """Verify probe times out on hanging binary and reports error status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hang_bin = create_hanging_stub(tmpdir, "hang_bin", sleep_s=5.0)
            probe_hang = self.droid.probe(binary_path=hang_bin, timeout_s=0.2)
            self.assertEqual(probe_hang.status, "error")
            self.assertEqual(probe_hang.details.get("error"), "timeout")
            self.assertIn("timed out", probe_hang.details.get("stderr", ""))

    def test_probe_flag_diff_and_stale(self):
        """Verify probe detects help sha256 mismatch, reports flag_diff, and marks verification stale."""
        with tempfile.TemporaryDirectory() as tmpdir:
            alt_bin = create_modified_help_stub(tmpdir, "alt_bin", version="1.5.2", help_text="Altered agy help without flags")
            probe_alt = self.agy.probe(binary_path=alt_bin)
            self.assertEqual(probe_alt.status, "partially_verified")
            self.assertIsNotNone(probe_alt.flag_diff)
            self.assertFalse(probe_alt.flag_diff["match"])
            # Capabilities should be marked stale due to help hash mismatch
            for cname, cinfo in probe_alt.capabilities.items():
                if cinfo["verification"] != "none":
                    self.assertEqual(cinfo["verification"], "stale")

    def test_probe_database_recording(self):
        """Verify record_adapter_probe stores rows in adapter_probes table."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE adapter_probes (
                adapter TEXT NOT NULL,
                probed_at TEXT NOT NULL,
                binary_path TEXT,
                version TEXT,
                help_sha256 TEXT,
                flags_json TEXT NOT NULL,
                models_json TEXT,
                status TEXT NOT NULL,
                PRIMARY KEY (adapter, probed_at)
            ) STRICT;"""
        )
        probe = self.droid.probe(conn=conn)
        row = conn.execute("SELECT adapter, version, status, help_sha256 FROM adapter_probes WHERE adapter = 'droid';").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "droid")
        self.assertEqual(row[1], "1.6.0")
        self.assertEqual(row[2], "partially_verified")
        self.assertEqual(row[3], EXPECTED_HELP_HASHES["droid"])
        conn.close()

    def test_normalize_help_text(self):
        """Verify normalization merges stdout/stderr, strips ANSI, and drops setlocale warnings."""
        sample_help = "Usage of agy:\n  --print   Print mode\n  --model   Model name\n"
        # 1. Stderr only with locale warning and ANSI escapes
        stderr_input = (
            "bash: warning: setlocale: LC_ALL: cannot change locale (en_US.UTF-8): No such file or directory\n"
            + "\x1b[1mUsage of agy:\x1b[0m\n  \x1b[32m--print\x1b[39m   Print mode\n  --model   Model name\n"
        )
        norm = normalize_help_text(stdout="", stderr=stderr_input)
        self.assertEqual(norm, sample_help)

        # 2. Stdout and stderr merged
        norm2 = normalize_help_text(stdout="Usage of agy:\n", stderr="  --print   Print mode\n  --model   Model name\n")
        self.assertEqual(norm2, sample_help)

    def test_find_missing_flags(self):
        """Verify flag presence checks distinguish prefixes and detect only genuinely missing flags."""
        text = "Usage of agy:\n  --print-timeout   Timeout in sec\n  --model   Model name\n"
        flags = ["--print", "--print-timeout", "--model", "--conversation"]
        missing = find_missing_flags(flags, text)
        # --print is genuinely absent even though --print-timeout is present!
        self.assertIn("--print", missing)
        self.assertNotIn("--print-timeout", missing)
        self.assertNotIn("--model", missing)
        self.assertIn("--conversation", missing)

    def test_probe_help_on_stderr_only(self):
        """Verify stub that prints help to stderr only results in matching help_sha256 and no missing flags."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stub = create_stderr_help_stub(tmpdir, "agy_stderr", version="1.5.2")
            probe = self.agy.probe(binary_path=stub)
            self.assertEqual(probe.status, "partially_verified")
            self.assertEqual(probe.help_sha256, EXPECTED_HELP_HASHES["agy"])
            # All flags present in stderr help -> flag_diff is None
            self.assertIsNone(probe.flag_diff)
            # Verification is not stale because version and hash match
            for cname, cinfo in probe.capabilities.items():
                if cinfo["verification"] != "none":
                    self.assertNotEqual(cinfo["verification"], "stale")

    def test_probe_help_missing_flag(self):
        """Verify stub lacking an emitted flag lists exactly it in flag_diff missing_flags."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stub = create_missing_flag_stub(
                tmpdir,
                "agy_missing_flag",
                version="1.5.2",
                missing_flag="--dangerously-skip-permissions",
                to_stderr=True,
            )
            probe = self.agy.probe(binary_path=stub)
            self.assertEqual(probe.status, "partially_verified")
            self.assertIsNotNone(probe.flag_diff)
            self.assertFalse(probe.flag_diff["match"])
            # missing_flags must list exactly the missing flag
            self.assertEqual(probe.flag_diff["missing_flags"], ["--dangerously-skip-permissions"])
            # Capabilities should be marked stale due to help hash mismatch
            for cname, cinfo in probe.capabilities.items():
                if cinfo["verification"] != "none":
                    self.assertEqual(cinfo["verification"], "stale")


PROFILES = ("readonly", "edit", "dev", "elevated", "unrestricted")
UNSUPPORTED = {
    "droid": set(),
    "agy": {"dev", "elevated"},
    "kiro": {"dev", "elevated"},
    "vibe": {"elevated"},
    "fake": set(),
}
# Plan §14.2 profile-flag table, hard-coded.
PROFILE_FLAGS = {
    "droid": {"readonly": [], "edit": ["--auto", "low"], "dev": ["--auto", "medium"],
              "elevated": ["--auto", "high"], "unrestricted": ["--skip-permissions-unsafe"]},
    "agy": {"readonly": ["--mode", "plan"], "edit": ["--mode", "accept-edits"],
            "unrestricted": ["--dangerously-skip-permissions"]},
    "kiro": {"readonly": ["--trust-tools="], "edit": ["--trust-tools=fs_read,fs_write"],
             "unrestricted": ["--trust-all-tools"]},
    "vibe": {"readonly": ["--agent", "ask"], "edit": ["--agent", "accept-edits"],
             "dev": ["--agent", "smart-approve"], "unrestricted": ["--agent", "auto-approve", "--auto-approve"]},
    "fake": {p: ["--fake-profile", p] for p in PROFILES},
}
# Plan §14.3–§14.6 "argv built" templates plus §14.2 profile flags: the only flags each vendor
# adapter may emit (N15 / G24 "only template flags").
TEMPLATE_ALLOW = {
    "droid": {"--output-format", "--cwd", "--model", "--reasoning-effort", "--session-id",
              "--log-group-id", "--file", "--auto", "--skip-permissions-unsafe"},
    "agy": {"--print", "--output-format", "--model", "--effort", "--sandbox", "--agent", "--add-dir",
            "--conversation", "--print-timeout", "--mode", "--dangerously-skip-permissions"},
    "kiro": {"--no-interactive", "--output-format", "--model", "--effort", "--resume-id", "--agent",
             "--trust-tools", "--trust-all-tools"},
    "vibe": {"--prompt-file", "--output", "--workdir", "--model", "--resume", "--trust", "--time-limit",
             "--output-dir", "--max-turns", "--max-price", "--max-tokens", "--agent", "--auto-approve"},
}
HELP_FIXTURES = {
    "droid": ("droid", "1.3.0", "exec-help.txt"),
    "agy": ("agy", "1.5.2", "help.txt"),
    "kiro": ("kiro", "3.1.0", "chat-help.txt"),
    "vibe": ("vibe", "3.1.0", "help.txt"),
}
SUBCOMMANDS = {"droid": {"exec"}, "kiro": {"chat"}, "agy": set(), "vibe": set()}
MODELS = {"droid": ("claude-opus-5-5", "high"), "agy": ("claude-sonnet-4-6", "medium"),
          "kiro": ("claude-opus-5.5", "high"), "vibe": ("mistral-large", None), "fake": ("m-a", None)}
PROMPT_TEXT = "Task content"


def golden_argv(adapter, profile, resume, prompt, run_dir, options=None):
    """Hand-written golden argv per plan §14.3–§14.6 and §14.9 (independent of the adapter code)."""
    options = options or {}
    model, effort = MODELS[adapter]
    pf = PROFILE_FLAGS[adapter][profile]
    if adapter == "droid":
        argv = ["droid", "exec", "--output-format", "stream-json", "--cwd", "/ws/t", "--model", model,
                "--reasoning-effort", effort, *pf]
        if resume:
            argv += ["--session-id", resume]
        return argv + ["--log-group-id", "run_1", "--file", prompt]
    if adapter == "agy":
        argv = ["agy", "--print", "--output-format", "stream-json", "--model", model, "--effort", effort, *pf]
        if options.get("sandbox"):
            argv += ["--sandbox"]
        if options.get("agent"):
            argv += ["--agent", options["agent"]]
        if resume:
            argv += ["--conversation", resume]
        return argv + ["--print-timeout", "300s", PROMPT_TEXT]
    if adapter == "kiro":
        argv = ["kiro-cli", "chat", "--no-interactive", "--output-format", "stream-json", "--model", model,
                "--effort", effort, *pf]
        if resume:
            argv += ["--resume-id", resume]
        if options.get("agent"):
            argv += ["--agent", options["agent"]]
        return argv + [PROMPT_TEXT]
    if adapter == "vibe":
        argv = ["vibe", "--prompt-file", prompt, "--output", "streaming", "--workdir", "/ws/t"]
        if not resume:
            argv += ["--model", model]
        argv += pf
        if resume:
            argv += ["--resume", resume]
        if options.get("trust_workdir"):
            argv += ["--trust"]
        argv += ["--time-limit", "300", "--output-dir", os.path.join(run_dir, "vibe-out")]
        if "max_turns" in options:
            argv += ["--max-turns", str(options["max_turns"])]
        if "max_price_usd" in options:
            argv += ["--max-price", str(options["max_price_usd"])]
        if "max_tokens" in options:
            argv += ["--max-tokens", str(options["max_tokens"])]
        return argv
    if adapter == "fake":
        import sys as _sys
        argv = [_sys.executable, "-P", "-s", "-m", "mesh.testing.fake_agent", *pf]
        if resume:
            argv += ["--fake-resume", resume]
        return argv + [prompt]
    raise AssertionError(adapter)


EXTRA_OPTION_CASES = [
    ("agy", "edit", None, {"sandbox": True, "agent": "a1"}),
    ("agy", "readonly", "conv_1", {"sandbox": True}),
    ("kiro", "edit", "ks_1", {"agent": "ctx1"}),
    ("vibe", "dev", None, {"trust_workdir": True, "max_turns": 7, "max_price_usd": 2, "max_tokens": 9000}),
    ("vibe", "edit", "vs_1", {"trust_workdir": True}),
]


class TestAdapterGoldens(unittest.TestCase):
    """Full golden argv, allow-lists, profile separation and no vendor-config reads. ACC: N6, N15, G24, N7"""

    @classmethod
    def setUpClass(cls):
        cls.stubs_dir = tempfile.mkdtemp(prefix="unit-stubs-g-")
        create_test_stubs(cls.stubs_dir)
        cls.orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{cls.stubs_dir}:{cls.orig_path}"
        cls.adapters = {"droid": DroidAdapter(), "agy": AgyAdapter(), "kiro": KiroAdapter(),
                        "vibe": VibeAdapter(), "fake": FakeAdapter()}

    @classmethod
    def tearDownClass(cls):
        os.environ["PATH"] = cls.orig_path
        shutil.rmtree(cls.stubs_dir, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="unit-golden-")
        self.run_dir = self.tmp.name
        self.prompt = os.path.join(self.run_dir, "prompt.md")
        with open(self.prompt, "w", encoding="utf-8") as f:
            f.write(PROMPT_TEXT)

    def tearDown(self):
        self.tmp.cleanup()

    def _req(self, adapter, profile, resume=None, options=None, effort="__default__", model="__default__"):
        m, e = MODELS[adapter]
        opts = {"run_id": "run_1"}
        opts.update(options or {})
        return RunRequest(
            prompt_path=self.prompt, workspace="/ws/t",
            model=m if model == "__default__" else model,
            effort=e if effort == "__default__" else effort,
            profile=profile, resume_session_id=resume, timeout_s=300, options=opts,
        )

    def _all_cases(self):
        """Yield (adapter, profile, resume, options, golden argv) for every supported combination."""
        for adapter in ("droid", "agy", "kiro", "vibe", "fake"):
            for profile in PROFILES:
                if profile in UNSUPPORTED[adapter]:
                    continue
                for resume in (None, "sess_1"):
                    yield adapter, profile, resume, {}, golden_argv(adapter, profile, resume, self.prompt, self.run_dir)
        for adapter, profile, resume, opts in EXTRA_OPTION_CASES:
            yield adapter, profile, resume, opts, golden_argv(adapter, profile, resume, self.prompt, self.run_dir, opts)

    def test_fake_session_parse_last_json_object(self):
        """ACC: L5 - capture the last stdout object with session_id, not stderr."""
        adapter = FakeAdapter()
        self.assertIsNone(adapter.parse(self.run_dir).session_id)
        stdout = ('plain text\n{"session_id":"first"}\n[]\nnull\n'
                  '{"session_id":"last"}\n{"other":"ignored"}\n{"session_id":\n')
        with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write(stdout)
        with open(os.path.join(self.run_dir, "stderr.log"), "w", encoding="utf-8") as f:
            f.write('{"session_id":"not-stdout"}\n')
        parsed = adapter.parse(self.run_dir)
        self.assertEqual(parsed.session_id, "last")
        self.assertEqual(parsed.final_text, stdout)

    def test_fake_resume_golden_and_binary_options(self):
        """ACC: L5 - explicit resume and fresh goldens; configured binary wins."""
        import sys

        for resume, flags in ((None, []), ("sess_1", ["--fake-resume", "sess_1"])):
            with self.subTest(resume=resume):
                req = self._req("fake", "readonly", resume)
                expected = [sys.executable, "-P", "-s", "-m", "mesh.testing.fake_agent",
                            "--fake-profile", "readonly", *flags, self.prompt]
                self.assertEqual(FakeAdapter().build(req).argv, expected)
                req.options["binary"] = "/tmp/mh-options/python3"
                self.assertEqual(FakeAdapter().build(req).argv, [req.options["binary"], *expected[1:]])

    def test_droid_real_run_id_and_binary_options(self):
        """RunRequest options select droid's binary and the actual log group, offline."""
        req = self._req("droid", "readonly", options={
            "binary": "/tmp/mh-options/droid", "run_id": "run_actual"})
        self.assertEqual(DroidAdapter().build(req).argv, [
            "/tmp/mh-options/droid", "exec", "--output-format", "stream-json",
            "--cwd", "/ws/t", "--model", "claude-opus-5-5", "--reasoning-effort", "high",
            "--log-group-id", "run_actual", "--file", self.prompt])

    def test_daemon_run_options_and_resume_verification(self):
        """ACC: L5 - daemon options reach fake/droid; unknown or stale cannot resume."""
        from mesh.adapters.base import Capability
        from mesh.daemon.supervisor import build_run_options, verified_resume_session

        agent = {"binary_path": "/tmp/mh-options/agent",
                 "adapter_options_json": '{"resume_verified":true}'}
        options = build_run_options(agent, "run_from_daemon")
        self.assertEqual(options, {"binary": agent["binary_path"], "run_id": "run_from_daemon",
                                   "resume_verified": True})
        for name, adapter in (("fake", FakeAdapter()), ("droid", DroidAdapter())):
            req = self._req(name, "readonly")
            req.options = options
            argv = adapter.build(req).argv
            self.assertEqual(argv[0], agent["binary_path"])
            if name == "droid":
                self.assertEqual(argv[argv.index("--log-group-id") + 1], "run_from_daemon")
        self.assertEqual(build_run_options({"binary_path": None, "adapter_options_json": "{}"}, "run_x"),
                         {"run_id": "run_x"})
        fake = get_adapter("fake", testing_mode=True, options=options)
        self.assertEqual(verified_resume_session(fake, "session"), "session")
        self.assertIsNone(verified_resume_session(fake, None))
        for supported, verification in (("yes", "help"), ("yes", "stale"), ("unknown", "smoke"),
                                         ("unknown", "none"), ("no", "smoke")):
            fake.capabilities["resume"] = Capability("resume", supported, verification)
            self.assertIsNone(verified_resume_session(fake, "session"))
        fake.capabilities.pop("resume")
        self.assertIsNone(verified_resume_session(fake, "session"))

    def test_adapters_build_golden_matrix(self):
        """ACC: N6 - exact golden argv for every adapter x profile x resume (plus option cases)."""
        n = 0
        for adapter, profile, resume, opts, golden in self._all_cases():
            with self.subTest(adapter=adapter, profile=profile, resume=resume, options=opts):
                spec = self.adapters[adapter].build(self._req(adapter, profile, resume, opts))
                self.assertEqual(spec.argv, golden)
                self.assertEqual(spec.cwd, "/ws/t")
                self.assertEqual(spec.stdin, "devnull")
                argv = spec.argv
                for bad in ("-c", "--continue", "-w", "--worktree", "-r", "--last"):
                    self.assertNotIn(bad, argv)
                if adapter in ("agy", "kiro"):
                    self.assertFalse(any(a in ("-w", "--worktree") or a.startswith("--worktree=") for a in argv))
                # A bare --resume (vibe only) must always carry an id.
                if "--resume" in argv:
                    i = argv.index("--resume")
                    self.assertEqual(adapter, "vibe")
                    self.assertEqual(argv[i + 1], resume)
                    self.assertNotIn("--model", argv, "vibe omits --model with --resume (plan §14.6)")
                n += 1
        self.assertEqual(n, 2 * (5 + 3 + 3 + 4 + 5) + len(EXTRA_OPTION_CASES))
        # Unsupported combinations raise, with and without resume.
        for adapter, profiles in UNSUPPORTED.items():
            for profile in profiles:
                for resume in (None, "sess_1"):
                    with self.subTest(adapter=adapter, profile=profile, resume=resume):
                        with self.assertRaises(ProfileUnsupported):
                            self.adapters[adapter].build(self._req(adapter, profile, resume))

    def test_adapter_flags_in_fixtures_goldens(self):
        """ACC: N15 - every flag in every golden argv is in the adapter's template allow-list and help fixture."""
        import re as _re

        helps = {}
        for adapter, parts in HELP_FIXTURES.items():
            with open(os.path.join(FIXTURES_ROOT, *parts), "r", encoding="utf-8") as f:
                helps[adapter] = f.read()
        checked = 0
        for adapter, profile, resume, opts, golden in self._all_cases():
            if adapter == "fake":
                continue
            spec = self.adapters[adapter].build(self._req(adapter, profile, resume, opts))
            self.assertEqual(spec.argv, golden)
            for i, tok in enumerate(golden[1:], start=1):
                if not tok.startswith("-"):
                    if i == 1 and tok in SUBCOMMANDS[adapter]:
                        self.assertRegex(helps[adapter] + "\n" + tok, _re.escape(tok))
                    continue
                flag = tok.split("=", 1)[0]
                with self.subTest(adapter=adapter, profile=profile, flag=flag):
                    self.assertIn(flag, TEMPLATE_ALLOW[adapter], f"{flag} not in {adapter} template allow-list")
                    self.assertRegex(
                        helps[adapter], r"(?<![\w-])" + _re.escape(flag) + r"(?![\w-])",
                        f"{flag} missing from {adapter} help fixture",
                    )
                    checked += 1
            # Non-flag subcommands: only the template subcommand appears in position 1.
            if SUBCOMMANDS[adapter]:
                self.assertIn(golden[1], SUBCOMMANDS[adapter])
            else:
                self.assertTrue(golden[1].startswith("-"))
        self.assertGreater(checked, 200)
        # vibe --trust appears only when trust_workdir is true.
        for profile in ("readonly", "edit", "dev", "unrestricted"):
            for resume in (None, "s1"):
                argv_f = self.adapters["vibe"].build(self._req("vibe", profile, resume, {"trust_workdir": False})).argv
                argv_n = self.adapters["vibe"].build(self._req("vibe", profile, resume)).argv
                argv_t = self.adapters["vibe"].build(self._req("vibe", profile, resume, {"trust_workdir": True})).argv
                self.assertNotIn("--trust", argv_f)
                self.assertNotIn("--trust", argv_n)
                self.assertEqual(argv_t.count("--trust"), 1)
        for adapter in ("droid", "agy", "kiro"):
            argv = self.adapters[adapter].build(self._req(adapter, "edit", None, {"trust_workdir": True})).argv
            self.assertNotIn("--trust", argv)

    def test_profile_flags_golden_and_separation(self):
        """ACC: G24 - plan §14.2 flags per profile, fake --fake-profile, ProfileUnsupported, effort/profile separation."""
        trust_all = {"--skip-permissions-unsafe", "--dangerously-skip-permissions", "--trust-all-tools", "--auto-approve"}
        for adapter, a in self.adapters.items():
            seen = {}
            for profile in PROFILES:
                with self.subTest(adapter=adapter, profile=profile):
                    if profile in UNSUPPORTED[adapter]:
                        with self.assertRaises(ProfileUnsupported) as ctx:
                            a.profile_flags(profile)
                        self.assertEqual(ctx.exception.code, c.PROFILE_UNSUPPORTED)
                        self.assertEqual(ctx.exception.exit_code, 13)
                        self.assertEqual(ctx.exception.details, {"adapter": adapter, "profile": profile})
                        with self.assertRaises(ProfileUnsupported):
                            a.build(self._req(adapter, profile))
                        continue
                    flags = a.profile_flags(profile)
                    self.assertEqual(flags, PROFILE_FLAGS[adapter][profile])
                    self.assertNotIn(tuple(flags), seen.values(), "distinct supported profiles need distinct flags")
                    seen[profile] = tuple(flags)
                    if adapter != "fake":
                        for f in flags:
                            if f in trust_all:
                                self.assertEqual(profile, "unrestricted")
                    argv = a.build(self._req(adapter, profile)).argv
                    if adapter == "fake":
                        i = argv.index("--fake-profile")
                        self.assertEqual(argv[i + 1], profile)
                        self.assertEqual(argv.count("--fake-profile"), 1)
                    else:
                        for f in trust_all:
                            if profile != "unrestricted":
                                self.assertNotIn(f, argv)
                        for tok in argv[1:]:
                            if tok.startswith("-"):
                                self.assertIn(tok.split("=", 1)[0], TEMPLATE_ALLOW[adapter])

        # Effort and profile never cross: the effort value sits only after the effort flag and the
        # profile flags do not depend on the effort (and vice versa).
        effort_flag = {"droid": "--reasoning-effort", "agy": "--effort", "kiro": "--effort"}
        for adapter, eflag in effort_flag.items():
            a = self.adapters[adapter]
            for profile in PROFILES:
                if profile in UNSUPPORTED[adapter]:
                    continue
                pflags = PROFILE_FLAGS[adapter][profile]
                for effort in ("low", "medium", "high", "xhigh", "max", None):
                    with self.subTest(adapter=adapter, profile=profile, effort=effort):
                        argv = a.build(self._req(adapter, profile, effort=effort)).argv
                        if effort is None:
                            self.assertNotIn(eflag, argv)
                        else:
                            self.assertEqual(argv.count(eflag), 1)
                            self.assertEqual(argv[argv.index(eflag) + 1], effort)
                        for p in PROFILES:
                            if eflag in argv:
                                self.assertNotEqual(argv[argv.index(eflag) + 1], p)
                        # Profile flags appear exactly once, contiguous, unchanged by effort.
                        if pflags:
                            idx = [i for i in range(len(argv)) if argv[i:i + len(pflags)] == pflags]
                            self.assertEqual(len(idx), 1, argv)
                        rest = list(argv)
                        if eflag in rest:
                            j = rest.index(eflag)
                            del rest[j:j + 2]
                        base = a.build(self._req(adapter, profile, effort=None)).argv
                        self.assertEqual(rest, base, "effort must only add its own flag/value")
        # vibe has no effort flag: any effort is refused rather than mapped onto a profile.
        for profile in ("readonly", "edit", "dev", "unrestricted"):
            with self.assertRaises(EffortUnsupported):
                self.adapters["vibe"].build(self._req("vibe", profile, effort="high"))
        # fake: effort never shows up in argv; profile value only after --fake-profile.
        for effort in ("low", "high", None):
            argv = self.adapters["fake"].build(self._req("fake", "dev", effort=effort)).argv
            self.assertNotIn("low", argv)
            self.assertNotIn("high", argv)

    def test_models_read_no_vendor_config(self):
        """ACC: N7 - kiro models from `kiro-cli chat --list-models -f json`, vibe null; no vendor config is opened."""
        import builtins
        import io as _io
        from types import SimpleNamespace
        from unittest import mock

        import mesh.adapters.base as base_mod
        import mesh.adapters.kiro as kiro_mod
        from mesh.daemon.handlers.adapter import handle_adapter_models

        home = tempfile.mkdtemp(prefix="unit-n7-home-")
        vendor_dirs = [".factory", ".gemini", ".kiro", ".vibe", ".config/kiro", ".config/vibe"]
        for d in vendor_dirs:
            os.makedirs(os.path.join(home, d), exist_ok=True)
            with open(os.path.join(home, d, "config.toml"), "w", encoding="utf-8") as f:
                f.write("tripwire = true\n")
        opened = []
        real_open, real_os_open, real_io_open = builtins.open, os.open, _io.open
        real_listdir, real_scandir, real_stat = os.listdir, os.scandir, os.stat

        def rec(fn):
            def wrapper(path, *a, **kw):
                if isinstance(path, (str, bytes, os.PathLike)):
                    opened.append(os.fsdecode(path))
                return fn(path, *a, **kw)
            return wrapper

        calls = []
        real_rro = base_mod.run_readonly_cmd

        def rro(argv, *a, **kw):
            calls.append(list(argv))
            return real_rro(argv, *a, **kw)

        old_home = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            with mock.patch.object(builtins, "open", rec(real_open)), \
                    mock.patch.object(os, "open", rec(real_os_open)), \
                    mock.patch.object(_io, "open", rec(real_io_open)), \
                    mock.patch.object(os, "listdir", rec(real_listdir)), \
                    mock.patch.object(os, "scandir", rec(real_scandir)), \
                    mock.patch.object(os, "stat", rec(real_stat)), \
                    mock.patch.object(kiro_mod, "run_readonly_cmd", rro):
                def _user_ctx(adapter_id: str):
                    return SimpleNamespace(
                        cmd="adapter.models",
                        conn=None,
                        principal={"id": "user", "kind": "user", "name": "user"},
                        actor_kind="user",
                        via="direct",
                        relayed_by=None,
                        dry_run=False,
                        args={"adapter_id": adapter_id},
                    )

                kiro_models = KiroAdapter().list_models()
                vibe_models = VibeAdapter().list_models()
                h_kiro = handle_adapter_models(_user_ctx("kiro"), testing_mode=True)
                h_vibe = handle_adapter_models(_user_ctx("vibe"), testing_mode=True)
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            shutil.rmtree(home, ignore_errors=True)

        with open(os.path.join(FIXTURES_ROOT, "kiro", "3.1.0", "list-models.json"), "r", encoding="utf-8") as f:
            expected = [m["model_id"] for m in json.load(f)["models"]]
        self.assertEqual(kiro_models, expected)
        self.assertEqual(len(kiro_models), 22)
        self.assertEqual(h_kiro.data["models"], expected)
        self.assertIsNone(vibe_models)
        self.assertIsNone(h_vibe.data["models"])
        self.assertTrue(calls, "kiro list_models must run the vendor list command")
        for argv in calls:
            self.assertEqual(argv[1:], ["chat", "--list-models", "-f", "json"])
            self.assertEqual(os.path.basename(argv[0]), "kiro-cli")
        for path in opened:
            norm = path.replace(os.sep, "/")
            self.assertFalse(norm.startswith(home), f"vendor home path touched: {path}")
            for d in (".factory", ".gemini", ".kiro", ".vibe"):
                self.assertNotIn(f"/{d}/", norm + "/", f"vendor config path touched: {path}")


class TestDaemonRunOptionGuards(unittest.TestCase):
    def test_adapter_options_cannot_override_run_id_or_configured_binary(self):
        from mesh.daemon.supervisor import build_run_options

        stored = {"run_id": "wrong_run", "binary": "/tmp/mh-options/wrong",
                  "resume_verified": True}
        for binary in ("/tmp/mh-options/configured", None):
            with self.subTest(binary=binary):
                agent = {"binary_path": binary, "adapter_options_json": json.dumps(stored)}
                self.assertEqual(build_run_options(agent, "actual_run"), {
                    "run_id": "actual_run", "binary": binary or stored["binary"],
                    "resume_verified": True,
                })

    def test_vendor_resume_cannot_be_verified_by_agent_option(self):
        """ACC: L5 - an agent option cannot promote help-only vendor resume."""
        from mesh.daemon.supervisor import verified_resume_session

        for name in ("droid", "kiro"):
            with self.subTest(adapter=name):
                self.assertIsNone(verified_resume_session(
                    get_adapter(name, options={"resume_verified": True}), "s"))


if __name__ == "__main__":
    unittest.main()
