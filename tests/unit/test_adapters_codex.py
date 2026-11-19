"""CodexAdapter (Codex CLI 0.166.0): argv, profiles, stream parse, failure meanings, probe.

Offline: synthetic streams in tests/fixtures/synthetic/codex/ (hand-written from the official
`codex exec --json` docs and the no-credential stream in openai/codex#22570; never captured,
never smoke evidence) and stub binaries in temp dirs. No real codex runs, no network, no login.
Expected values are literals.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from mesh.adapters import helpdiff
from mesh.adapters.base import EffortNotAllowed, ProfileUnsupported, RunRequest
from mesh.adapters.codex import CodexAdapter
from mesh.core.prompts import parse_deliverable

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNTH = os.path.join(ROOT, "tests", "fixtures", "synthetic", "codex")
TID = "0199a213-81c0-7800-8aa1-bbab2a035a53"
FAKE_KEY = "sk-proj-FAKE_KEY_0000"


def synth(name):
    return os.path.join(SYNTH, name)


class _Tmp(unittest.TestCase):
    def tmpdir(self):
        d = tempfile.mkdtemp(prefix="mh-codex-")
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def run_dir(self, fixture=None, text=None):
        d = self.tmpdir()
        if fixture:
            shutil.copyfile(synth(fixture), os.path.join(d, "stdout.log"))
        if text is not None:
            with open(os.path.join(d, "stdout.log"), "w", encoding="utf-8") as f:
                f.write(text)
        return d

    def req(self, profile="readonly", model="gpt-6.1-sol", effort=None, resume=None, options=None):
        d = self.tmpdir()
        prompt = os.path.join(d, "prompt.md")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("--not-a-flag: task text\n")
        return RunRequest(prompt_path=prompt, workspace=os.path.join(d, "ws"), model=model, effort=effort,
                          profile=profile, resume_session_id=resume, options=dict(options or {}, run_id="run_x"))


class TestCodexBuild(_Tmp):
    def test_readonly_argv_exact(self):
        r = self.req(effort="high")
        spec = CodexAdapter().build(r)
        self.assertEqual(spec.argv, [
            "codex", "--ask-for-approval", "never", "exec", "--json", "--skip-git-repo-check",
            "--cd", r.workspace, "--model", "gpt-6.1-sol", "--config", "model_reasoning_effort=high",
            "--sandbox", "read-only", "-"])
        self.assertEqual(spec.cwd, r.workspace)
        self.assertEqual(spec.stdin, os.path.abspath(r.prompt_path))

    def test_prompt_never_in_argv(self):
        """The prompt is read from stdin ("-"): a prompt starting with '-' can never become a flag."""
        spec = CodexAdapter().build(self.req())
        self.assertEqual(spec.argv[-1], "-")
        self.assertFalse(any("not-a-flag" in a for a in spec.argv))

    def test_profiles(self):
        a = CodexAdapter()
        self.assertEqual(a.profile_flags("readonly"), ["--sandbox", "read-only"])
        self.assertEqual(a.profile_flags("dev"), ["--sandbox", "workspace-write"])
        self.assertEqual(a.profile_flags("unrestricted"), ["--dangerously-bypass-approvals-and-sandbox"])
        for p in ("edit", "elevated", "nonsense"):
            with self.assertRaises(ProfileUnsupported):
                a.profile_flags(p)

    def test_no_trust_all_below_unrestricted(self):
        for p in ("readonly", "dev"):
            argv = CodexAdapter().build(self.req(profile=p)).argv
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertNotIn("danger-full-access", argv)
            self.assertIn("never", argv)
        argv = CodexAdapter().build(self.req(profile="unrestricted")).argv
        self.assertNotIn("--ask-for-approval", argv)

    def test_effort_values(self):
        for e in ("low", "medium", "high", "xhigh", "max", "ultra"):
            self.assertIn(f"model_reasoning_effort={e}", CodexAdapter().build(self.req(effort=e)).argv)
        for e in ("minimal", "ultracode", "HIGH"):
            with self.assertRaises(EffortNotAllowed):
                CodexAdapter().build(self.req(effort=e))
        self.assertNotIn("--config", CodexAdapter().build(self.req(effort=None)).argv)

    def test_no_model_no_flag_and_resume(self):
        argv = CodexAdapter().build(self.req(model=None, resume="sess_1")).argv
        self.assertNotIn("--model", argv)
        self.assertEqual(argv[-3:], ["resume", "sess_1", "-"])

    def test_binary_option_resolved(self):
        d = self.tmpdir()
        path = os.path.join(d, "codex")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(path, 0o755)
        self.assertEqual(CodexAdapter().build(self.req(options={"binary": path})).argv[0], path)

    def test_helpdiff_emitted_flags(self):
        self.assertEqual(helpdiff.emitted_flags(CodexAdapter()), {
            "--ask-for-approval", "--json", "--skip-git-repo-check", "--cd", "--model", "--config", "--sandbox",
            "--dangerously-bypass-approvals-and-sandbox"})


class TestCodexParse(_Tmp):
    def parse(self, fixture=None, text=None):
        return CodexAdapter().parse(self.run_dir(fixture, text))

    def test_ok_stream(self):
        res = self.parse("synthetic_ok.jsonl")
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertEqual(res.session_id, TID)
        self.assertTrue(res.final_text.startswith("Checking the repository first.\n\nMESH_SMOKE_OK"))
        self.assertEqual(parse_deliverable(res.final_text)["summary"], "MESH_SMOKE_OK codex")
        self.assertEqual(res.usage, {"input_tokens": 24763, "cached_input_tokens": 24448, "output_tokens": 122,
                                     "reasoning_output_tokens": 40, "status": "completed", "commands": 1,
                                     "turns": 1})
        self.assertNotIn("Reading the task", res.final_text)  # reasoning is not the answer

    def test_failure_meanings(self):
        cases = {
            "synthetic_not_logged_in.jsonl": "auth_failed",
            "synthetic_quota.jsonl": "quota_exhausted",
            "synthetic_network.jsonl": "network_error",
            "synthetic_turn_failed.jsonl": "run_error",
            "synthetic_stopped.jsonl": "incomplete",
            # Cut mid-line without runwrap's log cap: killed mid-write -> incomplete (PR49 r2).
            "synthetic_truncated.jsonl": "incomplete",
        }
        for fixture, meaning in cases.items():
            with self.subTest(fixture=fixture):
                self.assertEqual(self.parse(fixture).vendor_exit_meaning, meaning)

    def test_auth_stream_details(self):
        res = self.parse("synthetic_not_logged_in.jsonl")
        self.assertEqual(res.usage["status"], "failed")
        self.assertEqual(res.usage["reconnects"], 2)
        self.assertEqual(res.usage["notices"], 1)
        self.assertTrue(res.usage["error_message"].startswith("unexpected status 401 Unauthorized"))
        self.assertEqual(res.final_text, "")

    def test_reconnects_are_not_failures(self):
        res = self.parse("synthetic_reconnect_ok.jsonl")
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertEqual(res.usage["reconnects"], 2)

    def test_runwrap_log_cap_is_truncated(self):
        with open(synth("synthetic_stopped.jsonl"), encoding="utf-8") as f:
            text = f.read() + '{"type":"item.comp\n[runwrap] log cap reached; output truncated\n'
        res = self.parse(text=text)
        self.assertEqual(res.vendor_exit_meaning, "output_truncated")
        self.assertTrue(res.usage["log_capped"])

    def test_robust_inputs(self):
        self.assertEqual(CodexAdapter().parse(self.tmpdir()).vendor_exit_meaning, "incomplete")
        for text in ("", "\xff\xfe garbage\n", "[1,2]\n", "null\n"):
            with self.subTest(text=text):
                res = self.parse(text=text)
                self.assertIn(res.vendor_exit_meaning, ("incomplete", "output_truncated"))
                self.assertEqual(res.final_text, "")

    def test_unsafe_thread_id_ignored(self):
        res = self.parse(text='{"type":"thread.started","thread_id":"--evil"}\n{"type":"turn.completed"}\n')
        self.assertIsNone(res.session_id)
        self.assertIsNone(res.vendor_exit_meaning)

    def test_stream_facts(self):
        with open(synth("synthetic_quota.jsonl"), encoding="utf-8") as f:
            facts = CodexAdapter.stream_facts(f.read())
        self.assertEqual(facts["session_id"], TID)
        self.assertEqual(facts["vendor_exit_meaning"], "quota_exhausted")
        self.assertFalse(facts["turn_completed"])


class TestCodexStderr(unittest.TestCase):
    def test_signatures(self):
        self.assertIsNone(CodexAdapter.parse_stderr_error(""))
        self.assertIsNone(CodexAdapter.parse_stderr_error("Reading prompt from stdin...\n"))
        err = CodexAdapter.parse_stderr_error("error: unexpected argument '--json' found\n\nUsage: codex exec [OPTIONS]\n")
        self.assertEqual(err["reason"], "cli_usage_error")
        self.assertEqual(CodexAdapter.parse_stderr_error("401 Unauthorized: Missing bearer")["reason"], "auth_failed")
        self.assertEqual(CodexAdapter.parse_stderr_error("You've hit your usage limit.")["reason"], "quota_exhausted")


STUB = r'''#!{python}
import sys
args = sys.argv[1:]
HELP = """Usage: codex [OPTIONS] [PROMPT]
  -a, --ask-for-approval <POLICY>
  -C, --cd <DIR>
  -m, --model <MODEL>
"""
EXEC_HELP = """Usage: codex exec [OPTIONS] [PROMPT]
      --json
      --skip-git-repo-check
  -C, --cd <DIR>
  -m, --model <MODEL>
  -c, --config <key=value>
  -s, --sandbox <SANDBOX_MODE>
      --dangerously-bypass-approvals-and-sandbox
"""
if args == ["--version"]:
    print({version!r}); sys.exit({version_rc})
if args == ["--help"]:
    print(HELP); sys.exit(0)
if args == ["exec", "--help"]:
    print(EXEC_HELP.replace({drop!r}, "")); sys.exit(0)
if args == ["login", "status"]:
    print({login!r}); sys.exit({login_rc})
sys.exit(99)
'''


class TestCodexProbe(_Tmp):
    def stub(self, version="codex-cli 0.166.0", version_rc=0, drop="@@none@@",
             login="Logged in using an API key - " + FAKE_KEY[:8] + "***", login_rc=0):
        d = self.tmpdir()
        path = os.path.join(d, "codex")
        with open(path, "w", encoding="utf-8") as f:
            f.write(STUB.format(python=sys.executable, version=version, version_rc=version_rc, drop=drop,
                                login=login, login_rc=login_rc))
        os.chmod(path, 0o755)
        return path

    def test_not_installed_is_clear_not_a_crash(self):
        p = CodexAdapter().probe(binary_path=os.path.join(self.tmpdir(), "missing-codex"))
        self.assertEqual(p.status, "not_installed")
        self.assertIsNone(p.version)
        self.assertIn("npm install -g @openai/codex", p.details["hint"])
        self.assertFalse(CodexAdapter().is_installed(os.path.join(self.tmpdir(), "missing-codex")))

    def test_probe_ok_and_auth_without_secret(self):
        with mock.patch.dict(os.environ, {"CODEX_API_KEY": FAKE_KEY}):
            p = CodexAdapter().probe(binary_path=self.stub())
        self.assertEqual((p.status, p.version), ("partially_verified", "0.166.0"))
        self.assertIsNone(p.flag_diff)
        self.assertEqual(p.details["auth"], {"method": "api_key", "logged_in": True, "exit_code": 0,
                                             "api_key_env": ["CODEX_API_KEY"]})
        self.assertEqual({k: v["verification"] for k, v in p.capabilities.items()},
                         {"headless": "help", "stream_output": "help", "model_selection": "help",
                          "reasoning_effort": "help", "resume": "none"})
        self.assertNotIn("FAKE_KEY", json.dumps(p.to_dict()))
        self.assertNotIn("sk-proj", json.dumps(p.to_dict()))

    def test_login_status_variants(self):
        self.assertEqual(CodexAdapter.parse_login_status(1, "Not logged in", "")["method"], "none")
        self.assertFalse(CodexAdapter.parse_login_status(1, "Not logged in", "")["logged_in"])
        self.assertEqual(CodexAdapter.parse_login_status(0, "Logged in using ChatGPT", "")["method"], "chatgpt")
        self.assertEqual(CodexAdapter.parse_login_status(0, "", "")["method"], "unknown")

    def test_missing_flag_is_reported(self):
        p = CodexAdapter().probe(binary_path=self.stub(drop="      --json\n"))
        self.assertEqual(p.flag_diff, {"match": False, "missing_flags": ["--json"]})
        self.assertEqual(p.capabilities["stream_output"]["verification"], "unverified")
        self.assertEqual(p.capabilities["model_selection"]["verification"], "help")

    def test_version_failures(self):
        self.assertEqual(CodexAdapter().probe(binary_path=self.stub(version_rc=2)).status, "error")
        p = CodexAdapter().probe(binary_path=self.stub(version="codex-cli dev"))
        self.assertEqual(p.status, "unverified")
        p = CodexAdapter().probe(binary_path=self.stub(version="codex-cli 0.170.1"))
        self.assertEqual(p.version, "0.170.1")
        self.assertIn("0.166.0", p.details["version_note"])

    def test_tripwire_shim_on_path_counts_as_not_installed(self):
        root = self.tmpdir()
        shims = os.path.join(root, "tests", "shims")
        other = os.path.join(root, "bin")
        os.makedirs(shims)
        os.makedirs(other)
        for d in (shims, other):
            with open(os.path.join(d, "codex"), "w") as f:
                f.write("#!/bin/sh\nexit 99\n")
            os.chmod(os.path.join(d, "codex"), 0o755)
        with mock.patch.dict(os.environ, {"PATH": shims + os.pathsep + other}):
            self.assertIsNone(CodexAdapter().resolve_binary())
            self.assertEqual(CodexAdapter().probe().status, "not_installed")
        with mock.patch.dict(os.environ, {"PATH": other}):
            self.assertEqual(CodexAdapter().resolve_binary(), os.path.join(other, "codex"))
        # A shim checked out without its exec bit still hides the real binary.
        os.chmod(os.path.join(shims, "codex"), 0o644)
        with mock.patch.dict(os.environ, {"PATH": shims + os.pathsep + other}):
            self.assertIsNone(CodexAdapter().resolve_binary())

    def test_list_models_is_user_supplied(self):
        self.assertIsNone(CodexAdapter().list_models(self.stub()))

    def test_opt_in_switch(self):
        self.assertIs(CodexAdapter.nonzero_exit_uses_stream_meaning, True)

class TestCodexPR49R1(unittest.TestCase):
    def _run(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "stdout.log"), "w", encoding="utf-8") as f:
            f.write(text)
        return CodexAdapter().parse(d)

    def test_recovered_reconnect_does_not_name_the_failure(self):
        """PR49 r1 P2: a recovered Reconnecting notice is not the cause of a later failure."""
        text = "\n".join([
            '{"type":"thread.started","thread_id":"0199a213-81c0-7800-8aa1-bbab2a035a53"}',
            '{"type":"turn.started"}',
            '{"type":"error","message":"Reconnecting... 1/5 (stream disconnected before completion)"}',
            '{"type":"turn.failed","error":{"message":"Your input exceeds the context window"}}',
        ]) + "\n"
        self.assertEqual(self._run(text).vendor_exit_meaning, "run_error")


class TestErrorTextClassificationPR49R1(unittest.TestCase):
    def test_no_false_positives(self):
        from mesh.adapters import vendor_stream as vs
        for text in ("unexpected status 400 Bad Request: request id: req_7f4291ab",
                     "The model produced 4290 tokens of invalid JSON",
                     "Command timed out after 2m",
                     "MCP server 'docs' transport closed",
                     "you are unauthorized to view this file in the repo",
                     "insufficient permissions to write credits.md"):
            with self.subTest(text=text):
                self.assertIsNone(vs.classify_error_text(text))
        for text, want in (("unexpected status 429 Too Many Requests", "rate_limited"),
                           ("HTTP 429", "rate_limited"),
                           ("401 Unauthorized", "auth_failed"),
                           ("request timed out", "network_error"),
                           ("API Error: 400 You have insufficient credits", "quota_exhausted"),
                           ("402 Insufficient Balance", "quota_exhausted")):
            with self.subTest(text=text):
                self.assertEqual(vs.classify_error_text(text), want)

    def test_bounded_redacts_before_cutting(self):
        """PR49 r1 P3: a key straddling the cut is redacted, not left as a short prefix."""
        from mesh.adapters import vendor_stream as vs
        key = "sk-ant-api03-" + "Q7x9Lm2Pq8Rt5Vw3Yz6Ab1Cd4Ef7Gh0Jk"
        text = "e" * (vs.ERROR_TEXT_MAX - 8) + " " + key + " tail"
        out = vs.bounded(text)
        self.assertNotIn("sk-ant-api03-Q7", out)
        self.assertNotIn(key[13:30], out)


class TestCodexProjectLayerPR49R3(unittest.TestCase):
    def _req(self, ws, profile):
        prompt = os.path.join(ws, "..", "prompt-%s.md" % os.path.basename(ws))
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("hi")
        self.addCleanup(os.unlink, prompt)
        return RunRequest(prompt_path=prompt, workspace=ws, model=None, effort=None, profile=profile,
                          timeout_s=60, resume_session_id=None, options={})

    def _ws(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def test_project_layer_refused_below_unrestricted(self):
        from mesh.adapters.base import AdapterUnsupported
        for layer in (".codex", "mcp.json"):
            for profile in ("readonly", "dev"):
                with self.subTest(layer=layer, profile=profile):
                    ws = self._ws()
                    if layer == ".codex":
                        os.mkdir(os.path.join(ws, ".codex"))
                        with open(os.path.join(ws, ".codex", "config.toml"), "w") as f:
                            f.write("[mcp_servers.x]\ncommand = \"sh\"\n")
                    else:
                        with open(os.path.join(ws, layer), "w") as f:
                            f.write("{}")
                    with self.assertRaises(AdapterUnsupported) as cm:
                        CodexAdapter().build(self._req(ws, profile))
                    self.assertEqual(cm.exception.details.get("reason"), "codex_project_layer_present")
                    # spawn_failed keeps only "Type: message", so the token must be in the message.
                    self.assertIn("codex_project_layer_present", str(cm.exception))
        ws = self._ws()
        os.mkdir(os.path.join(ws, ".codex"))
        CodexAdapter().build(self._req(ws, "unrestricted"))  # explicit full trust: allowed

    def test_clean_workspace_argv_unchanged(self):
        ws = self._ws()
        argv = CodexAdapter().build(self._req(ws, "readonly")).argv
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[-1], "-")


class TestCodexProbeHelpTimeoutPR49R3(unittest.TestCase):
    def test_help_timeout_reports_timeout(self):
        import mesh.adapters.codex as cx
        real = cx.run_readonly_cmd
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        binp = os.path.join(d, "codex")
        with open(binp, "w") as f:
            f.write("#!/bin/sh\necho codex-cli 0.166.0\n")
        os.chmod(binp, 0o755)

        def fake(argv, timeout_s=None, **kw):
            if argv[1:] == ["--help"]:
                res = real(["/bin/sh", "-c", "exit 0"], timeout_s=timeout_s)
                res.returncode = -1
                return res
            return real(argv, timeout_s=timeout_s, **kw)
        with mock.patch.object(cx, "run_readonly_cmd", side_effect=fake):
            p = CodexAdapter().probe(binary_path=binp)
        self.assertEqual(p.status, "error")
        self.assertEqual(p.details["error"], "timeout")


class TestCodexAncestorLayerPR49G(unittest.TestCase):
    """PR49 gemini P2: Codex loads .codex/ from the git root down to the cwd, so a layer in a
    parent repo of a plain (non-git) task workspace counts too."""

    def _req(self, ws, profile="readonly"):
        prompt = os.path.join(self.root, "prompt.md")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("hi")
        return RunRequest(prompt_path=prompt, workspace=ws, model=None, effort=None, profile=profile,
                          timeout_s=60, resume_session_id=None, options={})

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_parent_repo_layer_refused(self):
        from mesh.adapters.base import AdapterUnsupported
        repo = os.path.join(self.root, "repo")
        ws = os.path.join(repo, ".mesh", "workspaces", "prj_1", "tasks", "tsk_1")
        os.makedirs(ws)
        os.mkdir(os.path.join(repo, ".git"))
        os.mkdir(os.path.join(repo, ".codex"))
        with self.assertRaises(AdapterUnsupported) as cm:
            CodexAdapter().build(self._req(ws))
        self.assertIn("codex_project_layer_present", str(cm.exception))
        CodexAdapter().build(self._req(ws, "unrestricted"))

    def test_layer_above_git_root_or_without_git_is_ignored(self):
        # .codex above the nearest .git is outside the project: not loaded, not refused.
        outer = os.path.join(self.root, "outer")
        ws = os.path.join(outer, "repo", "sub")
        os.makedirs(ws)
        os.mkdir(os.path.join(outer, ".codex"))
        os.mkdir(os.path.join(outer, "repo", ".git"))
        CodexAdapter().build(self._req(ws))
        # No .git anywhere above: only the workspace itself is the project.
        plain = os.path.join(self.root, "plain", "ws")
        os.makedirs(plain)
        os.mkdir(os.path.join(self.root, "plain", ".codex"))
        CodexAdapter().build(self._req(plain))

    def test_codex_home_is_not_a_project_layer(self):
        home = os.path.join(self.root, "home")
        ws = os.path.join(home, "mesh", "ws")
        os.makedirs(ws)
        os.mkdir(os.path.join(home, ".git"))
        os.mkdir(os.path.join(home, ".codex"))
        with mock.patch.dict(os.environ, {"CODEX_HOME": os.path.join(home, ".codex")}):
            CodexAdapter().build(self._req(ws))


if __name__ == "__main__":
    unittest.main()
