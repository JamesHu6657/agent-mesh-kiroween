"""AgyAdapter argv and stderr classification for agy 1.5.12, where ``--print`` takes the prompt
as its value (fix/agy-1.5.12-print; ACC: N6, N16).

Offline: no agy binary runs. ``REAL_1_5_12_STDERR`` is the stderr of the failed first smoke
attempt of 2026-11-19 (agy 1.5.12 at Mesh commit 94436d1, rc 2, no model call). That attempt's
captures were committed in 44cca5c and then replaced in tests/fixtures/real/agy/1.5.12/ by the
passing rerun made with the argv tested here (tests/smoke/results/agy-1.5.12-2026-11-19.json), so
the stderr is a literal here; ``REAL_1_5_12_STDERR_SHA256`` is the sha256 its sidecar recorded.
The 1.5.3 evidence is the ``command`` of the real capture sidecars in tests/fixtures/real/agy/1.5.3/.
``read_argv_1_5_12`` is a test-local model of how 1.5.12 reads an argv, written from that stderr
and the flag list of the help text; it is not vendor code.
"""

import json
import os
import shutil
import tempfile
import unittest

from mesh.adapters import helpdiff
from mesh.adapters import vendor_stream as vs
from mesh.adapters.agy import PROMPT_ARGV_EVIDENCE_VERSIONS, AgyAdapter
from mesh.adapters.base import EXPECTED_HELP_HASHES, RunRequest
from mesh.platform.paths import temp_root
from tests import tierlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL_1_5_3 = os.path.join(REPO_ROOT, "tests", "fixtures", "real", "agy", "1.5.3")

REAL_1_5_12_STDERR = (
    'Error: --print took "--output-format" as its prompt, so the intended prompt was left as an '
    "argument and ignored.\n"
    "Attach the prompt to the flag (--print='your prompt') and move --output-format elsewhere on "
    "the command line.\n"
)
# sha256 of the first attempt's smoke-S-X-1-stderr.txt, from its sidecar (commit 44cca5c).
REAL_1_5_12_STDERR_SHA256 = "e8e77ec0f84e0c3aca2754bc8adb8dfb2ad42ab03d023a15eb619363aae973d4"
POINTER = "Read the file {} and follow the instructions in it exactly. It is your complete task."

# Flags AgyAdapter.build() may emit besides --print, split by whether the help text gives them a value.
VALUE_FLAGS = ("--output-format", "--model", "--effort", "--mode", "--agent", "--add-dir", "--conversation",
               "--print-timeout")
BOOL_FLAGS = ("--sandbox", "--dangerously-skip-permissions")


def read_argv_1_5_12(args):
    """Model of agy 1.5.12: ``--print`` and every VALUE_FLAGS flag take the next item, whatever
    it looks like; anything else that is not a flag is a left-over argument. Returns
    ``(values, leftover, error)``; ``error`` is the first stderr line when ``--print`` took a
    flag-looking item and an argument was left over."""
    values, leftover, i = {}, [], 0
    while i < len(args):
        item = args[i]
        if item == "--print" or item in VALUE_FLAGS:
            assert i + 1 < len(args), f"{item} has no value"
            assert item not in values, f"{item} given twice"
            values[item] = args[i + 1]
            i += 2
        elif item in BOOL_FLAGS:
            values[item] = True
            i += 1
        else:
            assert not item.startswith("-"), f"flag outside the model: {item}"
            leftover.append(item)
            i += 1
    error = None
    took = values.get("--print")
    if leftover and isinstance(took, str) and took.startswith("-"):
        error = (f'Error: --print took "{took}" as its prompt, so the intended prompt was left as an '
                 "argument and ignored.")
    return values, leftover, error


class AgyPrintBase(unittest.TestCase):
    def setUp(self):
        self.agy = AgyAdapter()
        self.tmp = tempfile.mkdtemp(prefix="mh-agyprint-", dir=temp_root())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def prompt(self, text, name="prompt.md"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def req(self, path, **kw):
        base = dict(prompt_path=path, workspace="/ws/t", model="claude-sonnet-4-6", effort=None,
                    profile="readonly", timeout_s=300)
        base.update(kw)
        return RunRequest(**base)


class AgyPrintArgvTest(AgyPrintBase):
    def test_short_prompt_exact_argv(self):
        """ACC: N6 - short prompt: the prompt is the item directly after --print, then --output-format stream-json, and --print-timeout is last."""
        spec = self.agy.build(self.req(self.prompt("Task content"), effort="medium"))
        self.assertEqual(spec.argv, [
            "agy", "--print", "Task content", "--output-format", "stream-json", "--model", "claude-sonnet-4-6",
            "--effort", "medium", "--mode", "plan", "--print-timeout", "300s",
        ])
        self.assertEqual((spec.cwd, spec.stdin), ("/ws/t", "devnull"))

    def test_large_prompt_exact_argv(self):
        """ACC: N16 - a prompt over max_argv_prompt_bytes: the pointer sentence is the --print value and --add-dir <run_dir> stays, in the full option order."""
        path = self.prompt("A" * (self.agy.max_argv_prompt_bytes + 1))
        spec = self.agy.build(self.req(path, profile="edit", resume_session_id="conv_1",
                                       options={"sandbox": True, "agent": "reviewer"}))
        self.assertEqual(spec.argv, [
            "agy", "--print", POINTER.format(path), "--output-format", "stream-json", "--model",
            "claude-sonnet-4-6", "--mode", "accept-edits", "--sandbox", "--agent", "reviewer",
            "--add-dir", self.tmp, "--conversation", "conv_1", "--print-timeout", "300s",
        ])
        # Exactly at the limit the prompt text itself is still passed, with no --add-dir.
        at_limit = self.prompt("B" * self.agy.max_argv_prompt_bytes, "at-limit.md")
        argv = self.agy.build(self.req(at_limit)).argv
        self.assertEqual(argv[1:3], ["--print", "B" * self.agy.max_argv_prompt_bytes])
        self.assertNotIn("--add-dir", argv)

    def test_output_format_is_not_read_as_the_prompt_on_1_5_12(self):
        """ACC: N6 - read as agy 1.5.12 reads it, the new argv gives --print the prompt and --output-format stream-json with nothing left over; the earlier argv shape reproduces the real stderr."""
        old = ["--print", "--output-format", "stream-json", "--model", "claude-sonnet-4-6", "--mode", "plan",
               "--print-timeout", "300s", "Task content"]
        values, leftover, error = read_argv_1_5_12(old)
        self.assertEqual(values["--print"], "--output-format")
        self.assertIn("Task content", leftover)
        self.assertEqual(error, REAL_1_5_12_STDERR.splitlines()[0])

        argv = self.agy.build(self.req(self.prompt("Task content"))).argv
        self.assertEqual(argv[:5], ["agy", "--print", "Task content", "--output-format", "stream-json"])
        values, leftover, error = read_argv_1_5_12(argv[1:])
        self.assertEqual(values, {"--print": "Task content", "--output-format": "stream-json",
                                  "--model": "claude-sonnet-4-6", "--mode": "plan", "--print-timeout": "300s"})
        self.assertEqual((leftover, error), ([], None))

    def test_every_supported_combination_keeps_the_shape(self):
        """ACC: N6, N16 - profiles x resume x options x effort x prompt kind: one --print whose value is the prompt (never starting with "-", never attached with "="), then --output-format stream-json; nothing is left over for agy 1.5.12; --print-timeout <n>s closes the argv."""
        short = self.prompt("Task content")
        dash = self.prompt("--dangerously-skip-permissions do it", "dash.md")
        large = self.prompt("C" * (self.agy.max_argv_prompt_bytes + 1), "large.md")
        expected_prompt = {short: "Task content", dash: "\n--dangerously-skip-permissions do it",
                           large: POINTER.format(large)}
        n = 0
        for path in (short, dash, large):
            for profile in ("readonly", "edit", "unrestricted"):
                for resume in (None, "conv_9"):
                    for options in ({}, {"sandbox": True}, {"agent": "a1"}, {"sandbox": True, "agent": "a1"}):
                        for effort in (None, "high"):
                            with self.subTest(path=os.path.basename(path), profile=profile, resume=resume,
                                              options=options, effort=effort):
                                argv = self.agy.build(self.req(path, profile=profile, resume_session_id=resume,
                                                               options=dict(options), effort=effort)).argv
                                self.assertEqual(argv[:2], ["agy", "--print"])
                                self.assertEqual(argv[2], expected_prompt[path])
                                self.assertFalse(argv[2].startswith("-"))
                                self.assertEqual(argv[3:5], ["--output-format", "stream-json"])
                                self.assertEqual(argv[-2:], ["--print-timeout", "300s"])
                                self.assertEqual(argv.count("--print"), 1)
                                self.assertFalse([a for a in argv if a.startswith("--print=")])
                                values, leftover, error = read_argv_1_5_12(argv[1:])
                                self.assertEqual((leftover, error), ([], None))
                                self.assertEqual(values["--print"], expected_prompt[path])
                                self.assertEqual(values["--output-format"], "stream-json")
                                self.assertEqual(values.get("--conversation"), resume)
                                self.assertEqual(values.get("--effort"), effort)
                                self.assertEqual(values.get("--add-dir"), self.tmp if path == large else None)
                                self.assertEqual(values.get("--sandbox"), True if options.get("sandbox") else None)
                                self.assertEqual(values.get("--agent"), options.get("agent"))
                                self.assertEqual("--dangerously-skip-permissions" in values,
                                                 profile == "unrestricted")
                                self.assertEqual(values.get("--mode"),
                                                 {"readonly": "plan", "edit": "accept-edits"}.get(profile))
                                n += 1
        self.assertEqual(n, 3 * 3 * 2 * 4 * 2)

    def test_same_prompt_position_as_the_real_1_5_3_captures(self):
        """ACC: N6 - every real 1.5.3 stream capture records `agy -p <prompt> ...flags`: the prompt directly after the print flag and --output-format stream-json after it, the positions build() uses; the help lists -p as the alias of --print."""
        names = sorted(n for n in os.listdir(REAL_1_5_3) if n.startswith("stream-") and n.endswith(".jsonl"))
        self.assertEqual(len(names), 3)
        for name in names:
            command = tierlib.read_sidecar(os.path.join(REAL_1_5_3, name))["command"]
            with self.subTest(fixture=name):
                self.assertEqual(command[:3], ["agy", "-p", "<prompt>"])
                i = command.index("--output-format")
                self.assertGreater(i, 2)
                self.assertEqual(command[i + 1], "stream-json")
        with open(os.path.join(REAL_1_5_3, "help.txt"), encoding="utf-8") as f:
            help_text = f.read()
        self.assertRegex(help_text, r"(?m)^\s+-p\s+Short alias for --print$")
        argv = self.agy.build(self.req(self.prompt("Task content"))).argv
        self.assertEqual((argv[1], argv[2]), ("--print", "Task content"))
        self.assertGreater(argv.index("--output-format"), 2)

    def test_help_surface_and_evidence_versions(self):
        """ACC: N22 - the expected agy help hash is the 1.5.3 help fixture's (the 1.5.12 help text of 2026-11-19 is byte-identical, so it needs no new hash); every flag build() emits is in that help; the argv evidence names 1.5.3 and 1.5.12."""
        path = os.path.join(REAL_1_5_3, "help.txt")
        self.assertEqual(tierlib.read_sidecar(path)["sha256"], EXPECTED_HELP_HASHES["agy"])
        with open(path, encoding="utf-8") as f:
            tokens = helpdiff.flag_tokens(f.read())
        emitted = helpdiff.emitted_flags(self.agy)
        self.assertIn("--print", emitted)
        self.assertEqual(sorted(emitted - tokens), [])
        self.assertEqual(PROMPT_ARGV_EVIDENCE_VERSIONS, ("1.5.3", "1.5.12"))


class AgyUsageErrorTest(AgyPrintBase):
    def run_dir(self, stdout=None, stderr=None):
        d = tempfile.mkdtemp(prefix="run-", dir=self.tmp)
        for name, text in (("stdout.log", stdout), ("stderr.log", stderr)):
            if text is not None:
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write(text)
        return d

    def test_real_1_5_12_stderr_is_cli_usage_error(self):
        """ACC: N6 - the real 1.5.12 stderr (the literal has the sha256 the first attempt's capture sidecar recorded) is an argument rejection: reason cli_usage_error with the stderr text, the flag, the consumed item and a note naming the evidence versions."""
        self.assertEqual(tierlib.sha256_bytes(REAL_1_5_12_STDERR.encode("utf-8")), REAL_1_5_12_STDERR_SHA256)
        err = AgyAdapter.parse_stderr_error(REAL_1_5_12_STDERR)
        self.assertEqual(err["reason"], "cli_usage_error")
        details = err["details"]
        self.assertEqual(details["stderr"], REAL_1_5_12_STDERR.strip())
        self.assertEqual(details["flag"], "--print")
        self.assertEqual(details["consumed_as_prompt"], "--output-format")
        self.assertIn("agy rejected its arguments", details["note"])
        self.assertIn("1.5.3, 1.5.12", details["note"])
        self.assertEqual(json.loads(json.dumps(err)), err)

    def test_other_flag_spellings_and_bounds(self):
        """ACC: N6 - the same message naming the --prompt or -p alias, or another consumed item, is classified too; a long stderr is cut to the shared bound."""
        for flag, item in (("--prompt", "--model"), ("-p", "--mode")):
            text = f'Error: {flag} took "{item}" as its prompt, so the intended prompt was left as an argument and ignored.\n'
            with self.subTest(flag=flag):
                err = AgyAdapter.parse_stderr_error(text)
                self.assertEqual(err["reason"], "cli_usage_error")
                self.assertEqual((err["details"]["flag"], err["details"]["consumed_as_prompt"]), (flag, item))
        long_err = AgyAdapter.parse_stderr_error(REAL_1_5_12_STDERR + "x" * 5000)
        self.assertEqual(long_err["reason"], "cli_usage_error")
        self.assertLessEqual(len(long_err["details"]["stderr"]), vs.ERROR_TEXT_MAX + len("...[truncated]"))

    def test_not_a_usage_error(self):
        """ACC: N6 - None, empty or unrelated stderr gives None; an AGY_ERROR line keeps its existing result (no reason key), also on the real 1.5.3 network-error capture."""
        for text in (None, "", "\n", "error: something broke\n", "--print needs a value\n",
                     "the model took a while as its prompt was long\n", 42):
            with self.subTest(text=text):
                self.assertIsNone(AgyAdapter.parse_stderr_error(text))
        both = REAL_1_5_12_STDERR + 'AGY_ERROR: {"short_error":"boom","retryable":true}\n'
        self.assertEqual(AgyAdapter.parse_stderr_error(both), {"short_error": "boom", "retryable": True})
        path = os.path.join(REAL_1_5_3, "stream-step6-network-error-stderr.txt")
        tierlib.read_sidecar(path)
        with open(path, encoding="utf-8") as f:
            real = AgyAdapter.parse_stderr_error(f.read())
        self.assertNotIn("reason", real)
        self.assertEqual((real["retryable"], real["status"], real["error_code"]), (False, "UNKNOWN", 2))

    def test_only_agys_own_error_line_is_a_usage_error(self):
        """ACC: N6 - review of PR #61: the words "took ... as its prompt" inside other stderr text (a quoted prompt, tool output, a log line) are not an argument rejection, so the reason stays exit_nonzero; agy's own line, also indented or after other lines, still is."""
        from mesh.adapters.agy import _PROMPT_CONSUMED_RE
        from mesh.daemon.supervisor import _vendor_nonzero_failure

        self.assertTrue(_PROMPT_CONSUMED_RE.pattern.startswith("^"))
        not_agys = (
            'warning: tool output: the user said "--print took "x" as its prompt" in a file\nerror: network down\n',
            "step: agent -p took the file as its prompt\n",
            'log: Error: --print took "--output-format" as its prompt, quoted from the task text\n',
            "MyError: --print took 1 as its prompt\n",
        )
        for text in not_agys:
            with self.subTest(text=text):
                self.assertIsNone(AgyAdapter.parse_stderr_error(text))
                d = self.run_dir(stdout="", stderr=text)
                self.assertEqual(_vendor_nonzero_failure(d, "agy", 1, testing_mode=False), ("exit_nonzero", {}))
        for text in ("warming up\n" + REAL_1_5_12_STDERR, "  " + REAL_1_5_12_STDERR, REAL_1_5_12_STDERR.replace("\n", "\r\n")):
            with self.subTest(text=text):
                err = AgyAdapter.parse_stderr_error(text)
                self.assertEqual((err["reason"], err["details"]["flag"], err["details"]["consumed_as_prompt"]),
                                 ("cli_usage_error", "--print", "--output-format"))

    def test_unusable_agy_error_text_does_not_hide_the_rejection(self):
        """ACC: N6 - review of PR #61: the AGY_ERROR marker with no usable JSON line (cut off, not an object, or only mentioned mid-line) does not switch the rejection off; a usable AGY_ERROR line still wins."""
        for extra in ("AGY_ERROR: {not json\n", "AGY_ERROR: [1, 2]\n", "AGY_ERROR:\n", "see AGY_ERROR: lines below\n"):
            for text in (REAL_1_5_12_STDERR + extra, extra + REAL_1_5_12_STDERR):
                with self.subTest(text=text):
                    err = AgyAdapter.parse_stderr_error(text)
                    self.assertEqual((err["reason"], err["details"]["consumed_as_prompt"]),
                                     ("cli_usage_error", "--output-format"))
            with self.subTest(extra=extra):
                self.assertIsNone(AgyAdapter.parse_stderr_error("error: something broke\n" + extra))
        usable = 'AGY_ERROR: {"short_error":"boom","retryable":false}\n'
        for text in (REAL_1_5_12_STDERR + "AGY_ERROR: {not json\n" + usable, usable + REAL_1_5_12_STDERR):
            with self.subTest(text=text):
                self.assertEqual(AgyAdapter.parse_stderr_error(text), {"short_error": "boom", "retryable": False})

    def test_parse_and_supervisor_report_the_rejection(self):
        """ACC: N6 - a run that printed nothing and left the 1.5.12 stderr: parse() gives run_error with stderr_error.reason cli_usage_error; the supervisor's non-zero-exit reason is cli_usage_error (not exit_nonzero) with the stderr and note in its details."""
        from mesh.daemon.supervisor import _vendor_nonzero_failure

        d = self.run_dir(stdout="", stderr=REAL_1_5_12_STDERR)
        res = self.agy.parse(d)
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(res.usage["error"], "empty stdout")
        self.assertEqual(res.usage["stderr_error"]["reason"], "cli_usage_error")
        self.assertEqual((res.final_text, res.session_id), ("", None))
        for rc in (1, 2):
            reason, extra = _vendor_nonzero_failure(d, "agy", rc, testing_mode=False)
            with self.subTest(rc=rc):
                self.assertEqual(reason, "cli_usage_error")
                self.assertIn('--print took "--output-format" as its prompt', extra["stderr"])
                self.assertIn("agy rejected its arguments", extra["note"])
                self.assertEqual(extra["consumed_as_prompt"], "--output-format")
        plain = self.run_dir(stdout="", stderr="error: something broke\n")
        self.assertEqual(_vendor_nonzero_failure(plain, "agy", 1, testing_mode=False), ("exit_nonzero", {}))


if __name__ == "__main__":
    unittest.main()
