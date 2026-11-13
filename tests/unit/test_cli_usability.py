"""CLI usability fixes found while dogfooding (no daemon needed).

--version, help layout, plain-language help, `mesh help quickstart`, text rendering,
stdin for '-' flags, `agent token issue --out` destination checks, and doctor on an
uninitialised home.
"""

import argparse
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import mesh
import mesh.constants as c
from mesh.cli import stdin as cli_stdin
from mesh.cli.help import HELP_TOPICS, TOP_HELP_EPILOG
from mesh.cli.main import build_parser, main
from mesh.cli.render import render_text
from mesh.errors import MeshError
from tests.helpers import MeshHarness

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JARGON = re.compile(r"\bM[0-9](\.[0-9]+)?\b|M3-lite|§|\bplan section\b|in this milestone|"
                    r"partially_verified in|launch enabled in")


def _all_parsers(parser: argparse.ArgumentParser, path=("mesh",)):
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _all_parsers(sub, path + (name,))


def _run_main(*argv: str):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(list(argv))
    return rc, out.getvalue()


class TestVersionFlag(unittest.TestCase):
    def test_version_flag_matches_version_command(self) -> None:
        rc, out = _run_main("--version")
        self.assertEqual(rc, 0, out)
        env = json.loads(out)
        self.assertTrue(env["ok"])
        self.assertEqual(env["data"], {"version": mesh.__version__})
        rc2, out2 = _run_main("version")
        self.assertEqual(json.loads(out2)["data"], env["data"])

    def test_version_flag_text(self) -> None:
        rc, out = _run_main("--format", "text", "--version")
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"mesh {mesh.__version__}\n")

    def test_version_flag_real_binary(self) -> None:
        p = subprocess.run([os.path.join(REPO, "bin", "mesh"), "--version"], capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["data"]["version"], mesh.__version__)


class TestHelpLayout(unittest.TestCase):
    def test_every_parser_keeps_example_lines(self) -> None:
        for path, parser in _all_parsers(build_parser()):
            with self.subTest(cmd=" ".join(path)):
                self.assertTrue(issubclass(parser.formatter_class, argparse.RawDescriptionHelpFormatter))
                epilog = parser.epilog or ""
                examples = [ln for ln in epilog.splitlines() if ln.startswith("  mesh ")]
                text = parser.format_help()
                for ln in examples:
                    self.assertIn(ln + "\n", text + "\n")

    def test_adapter_help_examples_one_per_line(self) -> None:
        text = dict(_all_parsers(build_parser()))[("mesh", "adapter")].format_help()
        self.assertIn("Examples:\n  mesh adapter list\n  mesh adapter show droid\n", text)

    def test_task_cancel_help_says_reason_required(self) -> None:
        text = dict(_all_parsers(build_parser()))[("mesh", "task", "cancel")].format_help()
        self.assertIn("--reason is required", " ".join(text.split()))


class TestNoInternalJargon(unittest.TestCase):
    def test_parser_help_has_no_milestone_jargon(self) -> None:
        for path, parser in _all_parsers(build_parser()):
            texts = [parser.description or "", parser.epilog or ""]
            texts += [a.help or "" for a in parser._actions if isinstance(a.help, str)]
            for t in texts:
                with self.subTest(cmd=" ".join(path), text=t[:60]):
                    self.assertIsNone(JARGON.search(t), t)

    def test_help_topics_have_no_milestone_jargon(self) -> None:
        for topic, text in list(HELP_TOPICS.items()) + [("top", TOP_HELP_EPILOG)]:
            with self.subTest(topic=topic):
                self.assertIsNone(JARGON.search(text), JARGON.search(text))

    def test_not_implemented_message_is_plain_and_code_is_stable(self) -> None:
        from mesh.errors import NotImplementedInM1Error
        e = NotImplementedInM1Error("top")
        self.assertEqual(e.code, c.NOT_IMPLEMENTED_IN_M1)  # code unchanged
        self.assertIsNone(JARGON.search(e.message), e.message)

    def test_daemon_and_launch_messages_are_plain(self) -> None:
        for rel in ("mesh/daemon/server.py", "mesh/daemon/handlers/task.py", "mesh/daemon/lead_turns.py",
                    "mesh/errors.py"):
            with open(os.path.join(REPO, rel), encoding="utf-8") as f:
                src = f.read()
            for m in re.finditer(r'message=f?"([^"]*)"', src):
                with self.subTest(file=rel, msg=m.group(1)):
                    self.assertIsNone(JARGON.search(m.group(1)), m.group(1))
            self.assertNotIn("in this milestone", src)
            self.assertNotIn("lead turns in M3-lite", src)

    def test_usage_doc_status_line(self) -> None:
        with open(os.path.join(REPO, "docs", "usage.md"), encoding="utf-8") as f:
            head = "".join(f.readlines()[:8])
        self.assertNotIn("gate M2", head)
        self.assertNotIn("§", head)


class TestQuickstart(unittest.TestCase):
    def test_quickstart_topic(self) -> None:
        rc, out = _run_main("help", "quickstart")
        self.assertEqual(rc, 0, out)
        text = json.loads(out)["data"]["text"]
        for needle in ("mesh setup", "mesh agent onboard", "mesh task keepalive", "--out", "--repo",
                       "mesh task create", "--spec -", "mesh task start",
                       "mesh task submit", "mesh task accept", "mesh task merge", "mesh project deliver",
                       "Examples:"):
            self.assertIn(needle, text)

    def test_quickstart_listed_and_text_output(self) -> None:
        rc, out = _run_main("help")
        self.assertIn("quickstart", json.loads(out)["data"]["topics"])
        rc, out = _run_main("--format", "text", "help", "quickstart")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("Agent Mesh Quickstart\n"), out[:80])
        self.assertIn("mesh help quickstart", TOP_HELP_EPILOG)

    def test_quickstart_commands_parse(self) -> None:
        """Every `mesh ...` line in the quickstart is a valid command line."""
        parser = build_parser()
        text = HELP_TOPICS["quickstart"].replace("\\\n", " ")
        cmds = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("mesh ")]
        self.assertGreaterEqual(len(cmds), 10)
        for cmd in cmds:
            argv = cmd.split("#")[0].split("<")[0].split(">")[0]
            import shlex
            argv = shlex.split(argv.replace("$W", "w.tok"))[1:]
            with self.subTest(cmd=cmd):
                parser.parse_args(argv)


class TestTextRender(unittest.TestCase):
    def test_list_renders_table_not_json(self) -> None:
        items = [{"id": "droid", "kind": "vendor", "status": "partially_verified", "launch_enabled": False,
                  "launch_milestone": "M3", "launch_reason": "no_smoke_evidence", "version": "1.6.0"},
                 {"id": "none", "kind": "none", "status": "verified", "launch_enabled": False,
                  "launch_milestone": None, "launch_reason": None, "version": None}]
        text = render_text({"adapters": items, "items": items})
        lines = text.splitlines()
        self.assertEqual(len(lines), 3, text)  # header + one line per item, alias not repeated
        self.assertTrue(lines[0].startswith("ID"))
        self.assertNotIn("{", text)
        self.assertNotIn("M3", text)  # internal milestone column is not shown
        self.assertIn("droid", lines[1])
        self.assertIn("no_smoke_evidence", lines[1])

    def test_row_version_counter_hidden_product_version_shown(self) -> None:
        self.assertNotIn("VERSION", render_text({"items": [{"id": "agt_1", "name": "a", "version": 3}]}))
        self.assertIn("VERSION", render_text({"items": [{"id": "droid", "version": "1.6.0"}]}))

    def test_doctor_checks_table_shows_details(self) -> None:
        text = render_text({"status": "healthy", "checks": [{"name": "git", "ok": True, "detail": "git version 2"}]})
        self.assertIn("git", text)
        self.assertIn("yes", text)
        self.assertIn("git version 2", text)

    def test_empty_list(self) -> None:
        self.assertEqual(render_text({"items": []}), "(none)")

    def test_created_object_is_not_rendered_as_version(self) -> None:
        task = {"id": "tsk_1", "project_id": "prj_1", "title": "T", "spec": "line1\nline2", "status": "assigned",
                "version": 1, "depends_on": [], "history": [{"seq": 1, "type": "x"}], "failure": None}
        text = render_text(task)
        self.assertNotEqual(text.strip(), "mesh 1")
        lines = text.splitlines()
        self.assertEqual(lines[0], "id: tsk_1")
        self.assertIn("title: T", lines)
        self.assertIn("status: assigned", lines)
        self.assertIn("  line2", lines)
        self.assertIn("depends_on: (none)", lines)
        self.assertFalse(any(ln.startswith("failure") for ln in lines))  # null fields omitted

    def test_version_and_text_shapes(self) -> None:
        self.assertEqual(render_text({"version": "9"}), "mesh 9")
        self.assertEqual(render_text({"topic": "x", "text": "hello\n"}), "hello")

    def test_extra_keys_after_table(self) -> None:
        text = render_text({"items": [{"id": "a"}], "current_lead": "lead1", "next_cursor": None})
        self.assertIn("current_lead: lead1", text)
        self.assertNotIn("next_cursor", text)


class TestStdinOnce(unittest.TestCase):
    def setUp(self) -> None:
        cli_stdin.reset()
        self.addCleanup(cli_stdin.reset)

    def test_second_reader_is_refused(self) -> None:
        with mock.patch("sys.stdin", io.StringIO('{"a": 1}')):
            self.assertEqual(cli_stdin.read_stdin("--input"), '{"a": 1}')
            with self.assertRaises(MeshError) as cm:
                cli_stdin.read_stdin("--spec")
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
        self.assertIn("--input", cm.exception.message)

    def test_spec_dash_reads_stdin(self) -> None:
        from mesh.cli.commands.task import _spec_value
        ns = argparse.Namespace(spec="-")
        with mock.patch("sys.stdin", io.StringIO("multi\nline spec\n")):
            self.assertEqual(_spec_value(ns), "multi\nline spec\n")

    def test_spec_dash_from_json_is_literal(self) -> None:
        from mesh.cli.commands.task import _spec_value
        ns = argparse.Namespace(spec="-", _from_json={"spec"})
        with mock.patch("sys.stdin", io.StringIO("ignored")):
            self.assertEqual(_spec_value(ns), "-")

    def test_spec_dash_empty_stdin_refused(self) -> None:
        from mesh.cli.commands.task import _spec_value
        with mock.patch("sys.stdin", io.StringIO("  \n")):
            with self.assertRaises(MeshError) as cm:
                _spec_value(argparse.Namespace(spec="-"))
        self.assertEqual(cm.exception.details, {"param": "spec"})

    def test_input_and_spec_dash_conflict_via_main(self) -> None:
        with mock.patch("sys.stdin", io.StringIO('{"project": "prj_1", "title": "t"}')):
            rc, out = _run_main("--home", tempfile.mkdtemp(prefix="mh-"), "task", "create", "--input", "-",
                                "--spec", "-")
        env = json.loads(out)
        self.assertEqual(rc, c.EXIT_USAGE, env)
        self.assertEqual(env["error"]["code"], c.INVALID_ARGUMENT)
        self.assertIn("already used by --input", env["error"]["message"])


class TestTokenOutChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="mh-out-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = os.path.join(self.base, "home")
        os.mkdir(self.home, 0o700)

    def _check(self, path: str, force: bool = False) -> str:
        from mesh.cli.commands.agent import _check_out_path
        return _check_out_path(self.home, path, force)

    def _refused(self, path: str, needle: str, force: bool = False) -> None:
        with self.assertRaises(MeshError) as cm:
            self._check(path, force)
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
        self.assertIn(needle, cm.exception.message)

    def test_fresh_path_ok(self) -> None:
        p = os.path.join(self.base, "tok", "w.token")
        self.assertEqual(self._check(p), p)

    def test_existing_needs_force(self) -> None:
        p = os.path.join(self.base, "w.token")
        open(p, "w").close()
        self._refused(p, "already exists; use --force")
        self.assertEqual(self._check(p, force=True), p)

    def test_symlink_refused_even_with_force(self) -> None:
        p = os.path.join(self.base, "link.token")
        os.symlink(os.path.join(self.base, "target"), p)
        for force in (False, True):
            self._refused(p, "symlink", force)

    def test_directory_refused(self) -> None:
        self._refused(self.base, "is a directory")

    def test_inside_home_workspaces_or_runs_refused(self) -> None:
        for sub in ("workspaces", "runs"):
            self._refused(os.path.join(self.home, sub, "x.token"), "would be inside")

    def test_inside_git_work_tree_refused(self) -> None:
        os.mkdir(os.path.join(self.base, "repo"))
        os.mkdir(os.path.join(self.base, "repo", ".git"))
        self._refused(os.path.join(self.base, "repo", "x.token"), "git work tree")

    def test_group_or_world_writable_dir_refused(self) -> None:
        d = os.path.join(self.base, "shared")
        os.mkdir(d)
        os.chmod(d, 0o777)
        self._refused(os.path.join(d, "x.token"), "writable by other users")
        os.chmod(d, 0o1777)  # sticky (like /tmp): others cannot replace our file
        self.assertTrue(self._check(os.path.join(d, "x.token")))

    def test_force_without_out_refused(self) -> None:
        rc, out = _run_main("--home", self.home, "agent", "token", "issue", "w", "--force")
        env = json.loads(out)
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertEqual(env["error"]["details"], {"param": "force"})


class TestDoctorUninitialised(unittest.TestCase):
    def setUp(self) -> None:
        self.h = MeshHarness()
        self.addCleanup(self.h.teardown)

    def test_uninitialised_home_is_not_healthy(self) -> None:
        rc, env = self.h.cli("doctor")
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["status"], "not_initialized")
        self.assertIn("mesh setup", env["data"]["hint"])

    def test_initialised_home_is_healthy(self) -> None:
        rc, _ = self.h.init()
        self.assertEqual(rc, 0)
        rc, env = self.h.cli("doctor")
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["status"], "healthy")
        self.assertNotIn("hint", env["data"])


if __name__ == "__main__":
    unittest.main()
