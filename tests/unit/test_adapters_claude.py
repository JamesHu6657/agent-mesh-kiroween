"""ClaudeAdapter (Claude Code 2.1.325): argv, permission profiles, stream parse, failure meanings,
auth detection, probe.

Offline: synthetic streams in tests/fixtures/synthetic/claude/ (hand-written from the official
stream-json / Agent SDK message docs; never captured, never smoke evidence) and stub binaries in
temp dirs. No real claude runs, no network, no login. Expected values are literals.
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
from mesh.adapters.claude import ClaudeAdapter
from mesh.core.prompts import parse_deliverable

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNTH = os.path.join(ROOT, "tests", "fixtures", "synthetic", "claude")
SID = "5f2b1c7e-0000-4000-8000-000000000001"
FAKE_KEY = "sk-ant-FAKE_KEY_0000"


def synth(name):
    return os.path.join(SYNTH, name)


class _Tmp(unittest.TestCase):
    def tmpdir(self):
        d = tempfile.mkdtemp(prefix="mh-claude-")
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

    def req(self, profile="readonly", model="opus", effort=None, resume=None, options=None):
        d = self.tmpdir()
        prompt = os.path.join(d, "prompt.md")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("--not-a-flag: task text\n")
        return RunRequest(prompt_path=prompt, workspace=os.path.join(d, "ws"), model=model, effort=effort,
                          profile=profile, resume_session_id=resume, options=dict(options or {}, run_id="run_x"))


class TestClaudeBuild(_Tmp):
    def test_readonly_argv_exact(self):
        r = self.req(effort="high")
        spec = ClaudeAdapter().build(r)
        self.assertEqual(spec.argv, [
            "claude", "-p", "--output-format", "stream-json", "--verbose", "--model", "opus",
            "--effort", "high", "--setting-sources", "user", "--strict-mcp-config", "--permission-mode", "dontAsk",
            "--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"])
        self.assertEqual(spec.cwd, r.workspace)
        self.assertEqual(spec.stdin, os.path.abspath(r.prompt_path))

    def test_prompt_never_in_argv(self):
        spec = ClaudeAdapter().build(self.req(profile="edit"))
        self.assertFalse(any("not-a-flag" in a for a in spec.argv))
        # --allowedTools is variadic: its value must be the very last argv element.
        self.assertEqual(spec.argv[-2:], ["--allowedTools", "Edit(./**)"])

    def test_profiles(self):
        a = ClaudeAdapter()
        self.assertEqual(a.profile_flags("readonly"),
                         ["--setting-sources", "user", "--strict-mcp-config", "--permission-mode", "dontAsk",
                          "--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"])
        self.assertEqual(a.profile_flags("edit"),
                         ["--setting-sources", "user", "--strict-mcp-config", "--permission-mode", "dontAsk",
                          "--disallowedTools", "Bash,WebFetch,WebSearch,Edit(./.git),Edit(./.git/**),Edit(./.claude),Edit(./.claude/**)",
                          "--allowedTools", "Edit(./**)"])
        self.assertEqual(a.profile_flags("unrestricted"), ["--permission-mode", "bypassPermissions"])
        for p in ("dev", "elevated", "nonsense"):
            with self.assertRaises(ProfileUnsupported):
                a.profile_flags(p)

    def test_permission_checks_not_skipped_below_unrestricted(self):
        for p in ("readonly", "edit"):
            with self.subTest(profile=p):
                argv = ClaudeAdapter().build(self.req(profile=p)).argv
                self.assertNotIn("bypassPermissions", argv)
                self.assertNotIn("--dangerously-skip-permissions", argv)
                self.assertNotIn("auto", argv)
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--allowedTools", ClaudeAdapter().build(self.req(profile="readonly")).argv)
        self.assertNotIn("Bash", ClaudeAdapter().profile_flags("edit")[-1].split(","))
        # PR49 r1 P1: no profile below unrestricted may pre-approve a bare Bash, and settings
        # files (project/local, with their hooks) must not widen it: explicit denies + user-only.
        for p in ("readonly", "edit"):
            with self.subTest(profile=p):
                argv = ClaudeAdapter().build(self.req(profile=p)).argv
                if "--allowedTools" in argv:
                    self.assertNotIn("Bash", argv[argv.index("--allowedTools") + 1].split(","))
                denied = argv[argv.index("--disallowedTools") + 1].split(",")
                self.assertIn("Bash", denied)
                self.assertIn("WebFetch", denied)
                self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")
                self.assertIn("--strict-mcp-config", argv)
                self.assertIn("WebSearch", denied)
        with self.assertRaises(ProfileUnsupported):
            ClaudeAdapter().build(self.req(profile="dev"))

    def test_edit_allow_is_path_scoped(self):
        """PR49 gemini P1: no bare file-writing tool in the allowlist (a bare Edit/Write approves
        writes anywhere on the host); writes are scoped to the workspace, .git/.claude denied."""
        argv = ClaudeAdapter().build(self.req(profile="edit")).argv
        allow = argv[argv.index("--allowedTools") + 1].split(",")
        for rule in allow:
            self.assertRegex(rule, r"^[A-Za-z]+\(\./")
        for bare in ("Edit", "Write", "NotebookEdit"):
            self.assertNotIn(bare, allow)
        deny = argv[argv.index("--disallowedTools") + 1].split(",")
        self.assertIn("Edit(./.git/**)", deny)
        self.assertIn("Edit(./.claude/**)", deny)

    def test_effort_values(self):
        for e in ("low", "medium", "high", "xhigh", "max", "ultracode"):
            argv = ClaudeAdapter().build(self.req(effort=e)).argv
            self.assertEqual(argv[argv.index("--effort") + 1], e)
        for e in ("ultra", "minimal", "HIGH"):
            with self.assertRaises(EffortNotAllowed):
                ClaudeAdapter().build(self.req(effort=e))
        self.assertNotIn("--effort", ClaudeAdapter().build(self.req(effort=None)).argv)

    def test_no_model_no_flag_and_resume(self):
        argv = ClaudeAdapter().build(self.req(model=None, resume="sess_1")).argv
        self.assertNotIn("--model", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], "sess_1")
        self.assertLess(argv.index("--resume"), argv.index("--permission-mode"))

    def test_binary_option_resolved(self):
        d = self.tmpdir()
        path = os.path.join(d, "claude")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(path, 0o755)
        self.assertEqual(ClaudeAdapter().build(self.req(options={"binary": path})).argv[0], path)

    def test_helpdiff_emitted_flags(self):
        self.assertEqual(helpdiff.emitted_flags(ClaudeAdapter()), {
            "-p", "--output-format", "--verbose", "--model", "--effort", "--resume", "--permission-mode",
            "--allowedTools", "--disallowedTools", "--setting-sources", "--strict-mcp-config"})


class TestClaudeParse(_Tmp):
    def parse(self, fixture=None, text=None):
        return ClaudeAdapter().parse(self.run_dir(fixture, text))

    def test_ok_stream(self):
        res = self.parse("synthetic_ok.jsonl")
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertEqual(res.session_id, SID)
        self.assertTrue(res.final_text.startswith("MESH_SMOKE_OK\n```mesh-deliverable"))
        self.assertEqual(parse_deliverable(res.final_text)["summary"], "MESH_SMOKE_OK claude")
        self.assertEqual(res.usage, {"total_cost_usd": 0.0123, "duration_ms": 3613, "num_turns": 2,
                                     "input_tokens": 150, "output_tokens": 70, "cache_read_input_tokens": 4,
                                     "stop_reason": "end_turn", "status": "completed",
                                     "model": "claude-opus-5-5", "permission_mode": "dontAsk",
                                     "api_key_source": "none", "result_subtype": "success"})

    def test_failure_meanings(self):
        cases = {
            "synthetic_not_logged_in.jsonl": "auth_failed",
            "synthetic_quota.jsonl": "quota_exhausted",
            "synthetic_network.jsonl": "network_error",
            "synthetic_error.jsonl": "run_error",
            "synthetic_max_turns.jsonl": "turn_limit",
            "synthetic_stopped.jsonl": "incomplete",
            # Cut mid-line without runwrap's log cap: killed mid-write -> incomplete (PR49 r2).
            "synthetic_truncated.jsonl": "incomplete",
        }
        for fixture, meaning in cases.items():
            with self.subTest(fixture=fixture):
                self.assertEqual(self.parse(fixture).vendor_exit_meaning, meaning)

    def test_is_error_with_success_subtype_is_a_failure(self):
        """not_logged_in has subtype "success" but is_error true: must not count as a finish."""
        res = self.parse("synthetic_not_logged_in.jsonl")
        self.assertEqual(res.usage["status"], "auth_failed")
        self.assertEqual(res.usage["error_message"], "Not logged in · Please run /login")

    def test_rate_limit_category_without_quota_text(self):
        with open(synth("synthetic_quota.jsonl"), encoding="utf-8") as f:
            text = f.read().replace("You've hit your limit", "Slow down")
        self.assertEqual(self.parse(text=text).vendor_exit_meaning, "rate_limited")

    def test_billing_category_is_quota(self):
        with open(synth("synthetic_error.jsonl"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        a = json.loads(lines[1])
        a["error"] = "billing_error"
        lines[1] = json.dumps(a)
        self.assertEqual(self.parse(text="\n".join(lines) + "\n").vendor_exit_meaning, "quota_exhausted")

    def test_insufficient_credits_text_is_quota(self):
        """A prepaid gateway's 400 "insufficient credits" (no billing category) is quota, not run_error."""
        with open(synth("synthetic_error.jsonl"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        r = json.loads(lines[2])
        r["result"] = "API Error: 400 You have insufficient credits. Please add credits to continue."
        r["errors"] = []
        lines[2] = json.dumps(r)
        self.assertEqual(self.parse(text="\n".join(lines) + "\n").vendor_exit_meaning, "quota_exhausted")

    def test_auth_by_text_without_category(self):
        with open(synth("synthetic_not_logged_in.jsonl"), encoding="utf-8") as f:
            text = f.read().replace(',"error":"authentication_failed"', "")
        self.assertNotIn("authentication_failed", text)
        self.assertEqual(self.parse(text=text).vendor_exit_meaning, "auth_failed")

    def test_retry_without_response_is_network(self):
        """No error text at all: api_retry with error_status null still means the network."""
        with open(synth("synthetic_network.jsonl"), encoding="utf-8") as f:
            text = f.read().replace("API Error: Connection error.", "API Error")
        self.assertEqual(self.parse(text=text).vendor_exit_meaning, "network_error")
        no_retry = "\n".join(l for l in text.splitlines() if "api_retry" not in l) + "\n"
        self.assertEqual(self.parse(text=no_retry).vendor_exit_meaning, "run_error")

    def test_recovered_retry_is_not_network(self):
        """PR49 r1 P2: a retry followed by a clean reply recovered; the later failure decides."""
        with open(synth("synthetic_error.jsonl"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        retry = ('{"type":"system","subtype":"api_retry","attempt":1,"error_status":null,"error":"unknown",'
                 '"session_id":"5f2b1c7e-0000-4000-8000-000000000001"}')
        clean = ('{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]},'
                 '"parent_tool_use_id":null,"session_id":"5f2b1c7e-0000-4000-8000-000000000001"}')
        text = "\n".join(lines[:1] + [retry, clean] + lines[1:]) + "\n"
        self.assertEqual(self.parse(text=text).vendor_exit_meaning, "run_error")

    def test_retry_then_tool_use_only_reply_is_not_network(self):
        """PR49 r2 P2: a clean reply with only tool_use blocks also means the retry recovered."""
        sid = "5f2b1c7e-0000-4000-8000-000000000001"
        text = "\n".join([
            '{"type":"system","subtype":"init","session_id":"%s","model":"opus"}' % sid,
            '{"type":"system","subtype":"api_retry","attempt":1,"error_status":null,"error":"unknown","session_id":"%s"}' % sid,
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Read","input":{}}]},'
            '"parent_tool_use_id":null,"session_id":"%s"}' % sid,
            '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"x"}]},"session_id":"%s"}' % sid,
            '{"type":"result","subtype":"error_during_execution","is_error":true,"session_id":"%s",'
            '"errors":["Tool execution failed: boom"]}' % sid,
        ]) + "\n"
        self.assertEqual(self.parse(text=text).vendor_exit_meaning, "run_error")
        # An assistant error followed by a clean reply no longer names the later failure either.
        text2 = text.replace('"subtype":"api_retry","attempt":1,"error_status":null,"error":"unknown"',
                             '"subtype":"status"').replace(
            '{"type":"assistant","message":{"content":[{"type":"tool_use"',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"limit"}]},"parent_tool_use_id":null,'
            '"error":"rate_limit","session_id":"%s"}\n{"type":"assistant","message":{"content":[{"type":"tool_use"' % sid, 1)
        self.assertEqual(self.parse(text=text2).vendor_exit_meaning, "run_error")

    def test_subagent_and_tool_errors_do_not_name_the_failure(self):
        """PR49 r1 P2: a subagent's rate_limit and a tool's own HTTP 401 in errors[] are not the
        vendor's failure."""
        sid = "5f2b1c7e-0000-4000-8000-000000000001"
        text = "\n".join([
            '{"type":"system","subtype":"init","session_id":"%s","model":"opus"}' % sid,
            '{"type":"assistant","message":{"content":[{"type":"text","text":"x"}]},"parent_tool_use_id":"toolu_1",'
            '"error":"rate_limit","session_id":"%s"}' % sid,
            '{"type":"result","subtype":"error_during_execution","is_error":true,"session_id":"%s",'
            '"errors":["Tool execution failed: gh api returned HTTP 401 Unauthorized"]}' % sid,
        ]) + "\n"
        res = self.parse(text=text)
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(len(res.usage["errors"]), 1)

    def test_stream_facts_keeps_u2028_inside_a_line(self):
        """PR49 r1 P3: U+2028 inside JSON text is not a line break (parse() and smoke agree)."""
        with open(synth("synthetic_ok.jsonl"), encoding="utf-8") as f:
            text = f.read()
        text = text.replace('"result":"', '"result":"a\u2028b ', 1) if '"result":"' in text else text
        self.assertIn("\u2028", text)
        facts = ClaudeAdapter.stream_facts(text)
        self.assertTrue(facts["result_seen"])
        self.assertIsNone(facts["vendor_exit_meaning"])
        self.assertIsNone(self.parse(text=text).vendor_exit_meaning)

    def test_budget_subtype(self):
        with open(synth("synthetic_max_turns.jsonl"), encoding="utf-8") as f:
            text = f.read().replace("error_max_turns", "error_max_budget_usd")
        self.assertEqual(self.parse(text=text).vendor_exit_meaning, "budget_exceeded")

    def test_subagent_text_is_not_the_answer(self):
        with open(synth("synthetic_ok.jsonl"), encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if '"type":"result"' not in l]
        res = self.parse(text="\n".join(lines) + "\n")
        self.assertEqual(res.vendor_exit_meaning, "incomplete")
        self.assertIn("Reading the task.", res.final_text)
        self.assertNotIn("SUBAGENT_TEXT_NOT_FINAL", res.final_text)
        self.assertNotIn("SUBAGENT_TEXT_NOT_FINAL", self.parse("synthetic_ok.jsonl").final_text)

    def test_permission_denial_is_not_fatal(self):
        res = self.parse("synthetic_denied.jsonl")
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertEqual(res.usage["tool_denied"], 1)
        self.assertEqual(res.usage["denied_tools"], ["Bash"])
        self.assertEqual(res.usage["status"], "completed")

    def test_api_key_source_recorded(self):
        self.assertEqual(self.parse("synthetic_network.jsonl").usage["api_key_source"], "ANTHROPIC_API_KEY")
        self.assertEqual(self.parse("synthetic_network.jsonl").usage["api_retries"], 2)

    def test_runwrap_log_cap_is_truncated(self):
        with open(synth("synthetic_stopped.jsonl"), encoding="utf-8") as f:
            text = f.read() + '{"type":"assis\n[runwrap] log cap reached; output truncated\n'
        res = self.parse(text=text)
        self.assertEqual(res.vendor_exit_meaning, "output_truncated")
        self.assertTrue(res.usage["log_capped"])

    def test_robust_inputs(self):
        self.assertEqual(ClaudeAdapter().parse(self.tmpdir()).vendor_exit_meaning, "incomplete")
        for text in ("", "\xff\xfe garbage\n", "[1,2]\n", "null\n"):
            with self.subTest(text=text):
                res = self.parse(text=text)
                self.assertIn(res.vendor_exit_meaning, ("incomplete", "output_truncated"))
                self.assertEqual(res.final_text, "")

    def test_unsafe_session_id_ignored(self):
        res = self.parse(text='{"type":"system","subtype":"init","session_id":"--evil"}\n'
                              '{"type":"result","subtype":"success","is_error":false,"result":"x"}\n')
        self.assertIsNone(res.session_id)
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertEqual(res.final_text, "x")

    def test_stream_facts(self):
        with open(synth("synthetic_denied.jsonl"), encoding="utf-8") as f:
            facts = ClaudeAdapter.stream_facts(f.read())
        self.assertEqual(facts, {"session_id": SID, "model": "claude-opus-5-5", "init_seen": True,
                                 "result_seen": True, "result_subtype": "success", "vendor_exit_meaning": None,
                                 "permission_denials": 1})


class TestClaudeStderr(unittest.TestCase):
    def test_signatures(self):
        self.assertIsNone(ClaudeAdapter.parse_stderr_error(""))
        self.assertIsNone(ClaudeAdapter.parse_stderr_error("some debug line\n"))
        err = ClaudeAdapter.parse_stderr_error("error: unknown option '--outputformat'\n")
        self.assertEqual(err["reason"], "cli_usage_error")
        self.assertEqual(ClaudeAdapter.parse_stderr_error("Invalid API key · Fix external API key")["reason"],
                         "auth_failed")
        self.assertEqual(ClaudeAdapter.parse_stderr_error("Credit balance is too low")["reason"], "quota_exhausted")


class TestClaudeAuth(unittest.TestCase):
    def test_auth_methods(self):
        cases = {
            "api_key": "api_key", "api_key_helper": "api_key", "claude.ai": "subscription",
            "oauth_token": "subscription", "third_party": "third_party",
        }
        for raw, method in cases.items():
            with self.subTest(raw=raw):
                out = json.dumps({"loggedIn": True, "authMethod": raw, "email": "someone@example.com"})
                auth = ClaudeAdapter.parse_auth_status(0, out)
                self.assertEqual(auth, {"method": method, "auth_method": raw, "logged_in": True, "exit_code": 0})
        none = ClaudeAdapter.parse_auth_status(1, json.dumps({"loggedIn": False, "authMethod": "none"}))
        self.assertEqual(none, {"method": "none", "auth_method": "none", "logged_in": False, "exit_code": 1})

    def test_unparseable_auth_is_unknown_and_drops_text(self):
        auth = ClaudeAdapter.parse_auth_status(0, "token " + FAKE_KEY)
        self.assertEqual(auth["method"], "unknown")
        self.assertNotIn("FAKE_KEY", json.dumps(auth))
        self.assertEqual(ClaudeAdapter.parse_auth_status(0, json.dumps({"authMethod": "weird"}))["method"],
                         "unknown")


STUB = r'''#!{python}
import sys
args = sys.argv[1:]
HELP = """Usage: claude [options] [command] [prompt]
  -p, --print
  --output-format <format>
  --verbose
  --model <model>
  --effort <level>
  --permission-mode <mode>
  --allowedTools, --allowed-tools <tools...>
  --disallowedTools, --disallowed-tools <tools...>
  --setting-sources <sources>
  --strict-mcp-config
  -r, --resume [value]
"""
if args == ["--version"]:
    print({version!r}); sys.exit({version_rc})
if args == ["--help"]:
    print(HELP.replace({drop!r}, "")); sys.exit(0)
if args == ["auth", "status"]:
    print({auth!r}); sys.exit({auth_rc})
sys.exit(99)
'''


class TestClaudeProbe(_Tmp):
    def stub(self, version="2.1.325 (Claude Code)", version_rc=0, drop="@@none@@",
             auth=None, auth_rc=0):
        if auth is None:
            auth = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "someone@example.com",
                               "orgName": "Org"})
        d = self.tmpdir()
        path = os.path.join(d, "claude")
        with open(path, "w", encoding="utf-8") as f:
            f.write(STUB.format(python=sys.executable, version=version, version_rc=version_rc, drop=drop,
                                auth=auth, auth_rc=auth_rc))
        os.chmod(path, 0o755)
        return path

    def test_not_installed_is_clear_not_a_crash(self):
        p = ClaudeAdapter().probe(binary_path=os.path.join(self.tmpdir(), "missing-claude"))
        self.assertEqual(p.status, "not_installed")
        self.assertIsNone(p.version)
        self.assertIn("claude auth login", p.details["hint"])
        self.assertFalse(ClaudeAdapter().is_installed(os.path.join(self.tmpdir(), "missing-claude")))

    def test_probe_subscription_login(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True):
            p = ClaudeAdapter().probe(binary_path=self.stub())
        self.assertEqual((p.status, p.version), ("partially_verified", "2.1.325"))
        self.assertIsNone(p.flag_diff)
        self.assertEqual(p.details["auth"], {"method": "subscription", "auth_method": "claude.ai",
                                             "logged_in": True, "exit_code": 0, "credential_env": []})
        self.assertNotIn("someone@example.com", json.dumps(p.to_dict()))
        self.assertEqual({k: v["verification"] for k, v in p.capabilities.items()},
                         {"headless": "help", "stream_output": "help", "model_selection": "help",
                          "reasoning_effort": "help", "resume": "none"})

    def test_probe_api_key_login_without_secret(self):
        auth = json.dumps({"loggedIn": True, "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"})
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": FAKE_KEY}):
            p = ClaudeAdapter().probe(binary_path=self.stub(auth=auth))
        self.assertEqual(p.details["auth"]["method"], "api_key")
        self.assertEqual(p.details["auth"]["credential_env"], ["ANTHROPIC_API_KEY"])
        self.assertNotIn("FAKE_KEY", json.dumps(p.to_dict()))
        self.assertNotIn("sk-ant", json.dumps(p.to_dict()))

    def test_probe_not_logged_in(self):
        p = ClaudeAdapter().probe(binary_path=self.stub(auth=json.dumps({"loggedIn": False, "authMethod": "none"}),
                                                        auth_rc=1))
        self.assertEqual(p.details["auth"]["method"], "none")
        self.assertFalse(p.details["auth"]["logged_in"])

    def test_missing_flag_is_reported(self):
        p = ClaudeAdapter().probe(binary_path=self.stub(drop="  --effort <level>\n"))
        self.assertEqual(p.flag_diff, {"match": False, "missing_flags": ["--effort"]})
        self.assertFalse(ClaudeAdapter.parse_auth_status(0, "Logged in as someone")["logged_in"])
        self.assertEqual(p.capabilities["reasoning_effort"]["verification"], "unverified")
        self.assertEqual(p.capabilities["model_selection"]["verification"], "help")

    def test_version_failures(self):
        self.assertEqual(ClaudeAdapter().probe(binary_path=self.stub(version_rc=2)).status, "error")
        self.assertEqual(ClaudeAdapter().probe(binary_path=self.stub(version="Claude Code")).status, "unverified")
        p = ClaudeAdapter().probe(binary_path=self.stub(version="2.1.400 (Claude Code)"))
        self.assertEqual(p.version, "2.1.400")
        self.assertIn("2.1.325", p.details["version_note"])

    def test_tripwire_shim_on_path_counts_as_not_installed(self):
        root = self.tmpdir()
        shims = os.path.join(root, "tests", "shims")
        other = os.path.join(root, "bin")
        os.makedirs(shims)
        os.makedirs(other)
        for d in (shims, other):
            with open(os.path.join(d, "claude"), "w") as f:
                f.write("#!/bin/sh\nexit 99\n")
            os.chmod(os.path.join(d, "claude"), 0o755)
        with mock.patch.dict(os.environ, {"PATH": shims + os.pathsep + other}):
            self.assertIsNone(ClaudeAdapter().resolve_binary())
            self.assertEqual(ClaudeAdapter().probe().status, "not_installed")
        with mock.patch.dict(os.environ, {"PATH": other}):
            self.assertEqual(ClaudeAdapter().resolve_binary(), os.path.join(other, "claude"))
        # A shim checked out without its exec bit still hides the real binary.
        os.chmod(os.path.join(shims, "claude"), 0o644)
        with mock.patch.dict(os.environ, {"PATH": shims + os.pathsep + other}):
            self.assertIsNone(ClaudeAdapter().resolve_binary())

    def test_list_models_is_user_supplied(self):
        self.assertIsNone(ClaudeAdapter().list_models(self.stub()))

    def test_opt_in_switch(self):
        self.assertIs(ClaudeAdapter.nonzero_exit_uses_stream_meaning, True)


class TestClaudeStderrPR49G(unittest.TestCase):
    def test_invalid_prefix_is_not_cli_usage(self):
        """PR49 gemini P3: "Error: Invalid API key" is auth, "TypeError: Invalid URL" is nothing."""
        r = ClaudeAdapter.parse_stderr_error("Error: Invalid API key · Please run /login\n")
        self.assertNotEqual((r or {}).get("reason"), "cli_usage_error")
        r = ClaudeAdapter.parse_stderr_error("TypeError: Invalid URL\n")
        self.assertNotEqual((r or {}).get("reason"), "cli_usage_error")
        for usage in ("error: unknown option '--nope'", "error: option '--effort <level>' argument 'x' is invalid",
                      "error: invalid value for --model", "error: missing required argument 'prompt'"):
            with self.subTest(usage=usage):
                self.assertEqual(ClaudeAdapter.parse_stderr_error(usage + "\n")["reason"], "cli_usage_error")


class TestEffortPolicyPR49G(unittest.TestCase):
    def test_validate_resolved_effort_uses_cli_list(self):
        """PR49 gemini P2: AG step 5 / T6 reject a value the CLI does not accept."""
        from mesh.adapters.base import EffortNotAllowed
        from mesh.adapters.codex import CodexAdapter
        from mesh.core.policy import validate_resolved_effort
        for adapter, bad, good in ((ClaudeAdapter(), "ultra", "ultracode"), (CodexAdapter(), "ultracode", "ultra")):
            with self.subTest(adapter=adapter.id):
                with self.assertRaises(EffortNotAllowed) as cm:
                    validate_resolved_effort(adapter_id=adapter.id, resolved_model=None, resolved_effort=bad,
                                             allowed_efforts=[bad], adapter=adapter)
                self.assertEqual(cm.exception.details["reason"], "effort_not_supported_by_cli")
                validate_resolved_effort(adapter_id=adapter.id, resolved_model=None, resolved_effort=good,
                                         allowed_efforts=[good], adapter=adapter)


if __name__ == "__main__":
    unittest.main()
