"""Full review r7 (R4-P1-1): kiro and droid refuse a workspace that carries a project config
layer whose MCP servers/hooks would run outside the permission profile (as codex does)."""

import os
import tempfile
import unittest
from unittest import mock

from mesh.adapters.base import AdapterUnsupported, RunRequest
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.kiro import KiroAdapter


def _touch(path, text="{}\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class ProjectLayerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self._env = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home})
        self._env.start()
        # A task worktree: its `.git` is a file, so it is its own git root.
        self.ws = os.path.join(self.root, "workspaces", "prj_x", "tasks", "tsk_x")
        _touch(os.path.join(self.ws, ".git"), "gitdir: /nowhere/.git/worktrees/tsk_x\n")
        self.prompt = os.path.join(self.root, "prompt.md")
        _touch(self.prompt, "# Agent Mesh task\n")

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def req(self, profile):
        return RunRequest(prompt_path=self.prompt, workspace=self.ws, model="m", effort=None, profile=profile)

    def assertRefused(self, adapter, profile, reason):
        with self.assertRaises(AdapterUnsupported) as cm:
            adapter.build(self.req(profile))
        self.assertEqual(cm.exception.details.get("reason"), reason)

    def test_kiro_refuses_workspace_mcp_and_agents_below_unrestricted(self):
        for rel in (".kiro/settings/mcp.json", ".kiro/agents/default.json"):
            with self.subTest(rel=rel):
                p = os.path.join(self.ws, rel)
                _touch(p)
                for profile in ("readonly", "edit"):
                    self.assertRefused(KiroAdapter(), profile, "kiro_project_layer_present")
                KiroAdapter().build(self.req("unrestricted"))
                os.unlink(p)

    def test_droid_refuses_hooks_settings_and_mcp_below_unrestricted(self):
        for rel in (".factory/hooks.json", ".factory/hooks/hooks.json", ".factory/settings.json",
                    ".factory/settings.local.json", ".factory/mcp.json"):
            with self.subTest(rel=rel):
                p = os.path.join(self.ws, rel)
                _touch(p)
                for profile in ("readonly", "edit", "dev", "elevated"):
                    self.assertRefused(DroidAdapter(), profile, "droid_project_layer_present")
                DroidAdapter().build(self.req("unrestricted"))
                os.unlink(p)
                if rel.startswith(".factory/hooks/"):
                    os.rmdir(os.path.dirname(p))

    def test_droid_refuses_mcp_in_an_ancestor_directory(self):
        _touch(os.path.join(self.root, "workspaces", ".factory", "mcp.json"))
        self.assertRefused(DroidAdapter(), "readonly", "droid_project_layer_present")

    def test_instruction_only_content_and_clean_workspaces_launch(self):
        _touch(os.path.join(self.ws, ".kiro", "steering", "style.md"), "# style\n")
        _touch(os.path.join(self.ws, ".factory", "skills", "x", "SKILL.md"), "# skill\n")
        KiroAdapter().build(self.req("readonly"))
        DroidAdapter().build(self.req("readonly"))

    def test_user_config_dirs_are_not_a_project_layer(self):
        # A workspace under $HOME: ~/.factory/mcp.json and ~/.kiro/... are the user's own config.
        _touch(os.path.join(self.home, ".factory", "mcp.json"))
        _touch(os.path.join(self.home, ".kiro", "settings", "mcp.json"))
        ws = os.path.join(self.home, "w")
        _touch(os.path.join(ws, ".git"), "gitdir: /nowhere\n")
        req = RunRequest(prompt_path=self.prompt, workspace=ws, model="m", effort=None, profile="readonly")
        DroidAdapter().build(req)
        KiroAdapter().build(req)

    def test_a_layer_written_by_an_earlier_run_is_caught_on_the_next_build(self):
        DroidAdapter().build(self.req("edit"))  # run 1: clean
        _touch(os.path.join(self.ws, ".factory", "hooks.json"),
               '{"SessionStart": [{"hooks": [{"type": "command", "command": "id"}]}]}\n')
        self.assertRefused(DroidAdapter(), "edit", "droid_project_layer_present")  # run 2


if __name__ == "__main__":
    unittest.main()
