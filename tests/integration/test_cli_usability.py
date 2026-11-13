"""CLI usability fixes against a live daemon: text output for lists and mutations,
`task create --spec -`, and `agent token issue --out`."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import mesh.constants as c
from tests.helpers import MeshHarness
from tests.integration.test_submit_cli import create_agent
from tests.stubs import create_test_stubs


class TestCliUsabilityLive(unittest.TestCase):
    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.h = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.tok_lead = create_agent(self.h, "lead1", can_lead=True)
        self.tok_grok = create_agent(self.h, "grok", capabilities=["intake", "relay"])
        create_agent(self.h, "ibx")
        rc, _ = self.h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)
        rc, resp = self.h.cli("intake", "submit", "--title", "P", "--goal", "G", "--criterion", "C1",
                              "--lead", "lead1", token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        self.project_id = resp["data"]["id"]
        self.tokdir = tempfile.mkdtemp(prefix="mh-tok-")

    def tearDown(self) -> None:
        self.h.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)
        shutil.rmtree(self.tokdir, ignore_errors=True)

    def _text(self, *args: str, user: bool = False, token=None, stdin=None):
        rc, env = self.h.cli("--format", "text", *args, user=user, token=token, stdin=stdin)
        self.assertIn("raw_stdout", env, env)  # not JSON
        return rc, env["raw_stdout"]

    # 2. --format text for list commands
    def test_list_commands_render_text_tables(self) -> None:
        rc, resp = self.h.cli("task", "create", "--project", self.project_id, "--title", "Listed",
                              "--spec", "S", "--assignee", "ibx", token=self.tok_lead)
        self.assertEqual(rc, 0, resp)
        for args, user, token, needle in (
            (("adapter", "list"), True, None, "fake"),
            (("agent", "list"), True, None, "lead1"),
            (("project", "list"), False, self.tok_lead, self.project_id),
            (("task", "list", "--project", self.project_id), False, self.tok_lead, "Listed"),
            (("agent", "token", "list", "lead1"), True, None, "cred_"),
            (("config", "list"), True, None, "lead.default_agent"),
        ):
            with self.subTest(cmd=" ".join(args)):
                rc, out = self._text(*args, user=user, token=token)
                self.assertEqual(rc, 0, out)
                self.assertIn(needle, out)
                for line in out.splitlines():
                    self.assertFalse(line.lstrip().startswith("{"), out)

    def test_adapter_list_text_has_no_milestones(self) -> None:
        rc, out = self._text("adapter", "list", user=True)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.splitlines()[0].startswith("ID"), out)
        self.assertNotRegex(out, r"\bM[0-9]\b")

    # 8. --spec - reads stdin
    def test_task_create_spec_from_stdin(self) -> None:
        spec = "Line one of the spec.\nLine two with 'quotes' and \"more\".\n"
        rc, resp = self.h.cli("task", "create", "--project", self.project_id, "--title", "Stdin spec",
                              "--assignee", "ibx", "--spec", "-", token=self.tok_lead, stdin=spec)
        self.assertEqual(rc, 0, resp)
        # The whole multi-line stdin is the spec (the daemon trims trailing whitespace).
        self.assertEqual(resp["data"]["spec"], spec.rstrip())
        rc, shown = self.h.cli("task", "show", resp["data"]["id"], token=self.tok_lead)
        self.assertEqual(shown["data"]["spec"], spec.rstrip())

    def test_task_create_spec_dash_with_empty_stdin_refused(self) -> None:
        rc, resp = self.h.cli("task", "create", "--project", self.project_id, "--title", "Empty",
                              "--assignee", "ibx", "--spec", "-", token=self.tok_lead, stdin="")
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["details"]["param"], "spec")
        rc, lst = self.h.cli("task", "list", "--project", self.project_id, token=self.tok_lead)
        self.assertEqual(lst["data"]["items"], [])

    def test_project_complete_report_dash_still_reads_stdin(self) -> None:
        # Reaches the daemon with the stdin report (the project is not completable yet,
        # so any error must be about the project state, not an unreadable report).
        rc, resp = self.h.cli("project", "complete", self.project_id, "--report", "-",
                              token=self.tok_lead, stdin='{"criteria": []}')
        self.assertNotIn("Failed to read report", json.dumps(resp))

    # 9. text output for create/mutations
    def test_text_task_create_shows_the_task(self) -> None:
        rc, out = self._text("task", "create", "--project", self.project_id, "--title", "Text create",
                             "--spec", "S", "--assignee", "ibx", token=self.tok_lead)
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(out.strip(), "mesh 1")
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("id: tsk_"), out)
        self.assertIn("title: Text create", lines)
        tsk = lines[0].split(": ", 1)[1]
        rc, out = self._text("task", "cancel", tsk, "--reason", "not needed", token=self.tok_lead)
        self.assertEqual(rc, 0, out)
        self.assertIn(tsk, out)
        self.assertNotEqual(out.strip().split("\n")[0], "mesh 1")

    # 5. agent token issue --out
    def test_token_issue_out_writes_private_file(self) -> None:
        path = os.path.join(self.tokdir, "sub", "ibx.token")
        rc, resp = self.h.cli("agent", "token", "issue", "ibx", "--out", path, user=True)
        self.assertEqual(rc, 0, resp)
        self.assertNotIn("token", resp["data"])
        self.assertNotIn("mesh_at_", json.dumps(resp))  # no token value anywhere in the output
        self.assertEqual(resp["data"]["token_file"], path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        p = subprocess.run([self.h.bin_mesh, "--home", self.h.home, "whoami", "--token-file", path],
                           capture_output=True, text=True, env=self.h.get_env(), timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(json.loads(p.stdout)["data"]["name"], "ibx")

    def test_token_issue_out_refuses_existing_unless_force(self) -> None:
        path = os.path.join(self.tokdir, "ibx.token")
        with open(path, "w") as f:
            f.write("old\n")
        os.chmod(path, 0o600)
        _, before = self.h.cli("agent", "token", "list", "ibx", user=True)
        rc, resp = self.h.cli("agent", "token", "issue", "ibx", "--out", path, user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertIn("--force", resp["error"]["message"])
        with open(path) as f:
            self.assertEqual(f.read(), "old\n")
        _, after = self.h.cli("agent", "token", "list", "ibx", user=True)
        self.assertEqual(len(after["data"]["items"]), len(before["data"]["items"]))  # nothing issued

        rc, resp = self.h.cli("agent", "token", "issue", "ibx", "--out", path, "--force", user=True)
        self.assertEqual(rc, 0, resp)
        with open(path) as f:
            self.assertNotEqual(f.read(), "old\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_token_issue_out_refuses_workspace_location(self) -> None:
        path = os.path.join(self.h.home, "workspaces", "ibx.token")
        rc, resp = self.h.cli("agent", "token", "issue", "ibx", "--out", path, user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertFalse(os.path.lexists(path))

    def test_token_issue_text_out_has_no_token(self) -> None:
        path = os.path.join(self.tokdir, "t.token")
        rc, out = self._text("agent", "token", "issue", "ibx", "--out", path, user=True)
        self.assertEqual(rc, 0, out)
        with open(path) as f:
            tok = f.read().strip()
        self.assertNotIn(tok, out)
        self.assertIn(f"token_file: {path}", out)

    # 1/3. not-implemented message wording (code unchanged)
    def test_top_message_plain(self) -> None:
        rc, resp = self.h.cli("top")
        self.assertEqual(resp["error"]["code"], c.NOT_IMPLEMENTED_IN_M1)
        self.assertNotIn("M1", resp["error"]["message"])


if __name__ == "__main__":
    unittest.main()
