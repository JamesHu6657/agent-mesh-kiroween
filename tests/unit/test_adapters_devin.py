"""DevinAdapter (Devin CLI 3000.14.30821): argv, prompt file, permission profiles, project
layer refusal, plain-text parse, failure meanings, auth detection, probe.

Offline: error/auth texts in tests/fixtures/synthetic/devin/ are copied from real devin
output (2026-11-19, see docs/adapters-devin.md) with personal fields replaced; stub binaries
live in temp dirs. No real devin runs, no network, no login. Expected values are literals.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from mesh.adapters.base import AdapterUnsupported, EffortNotAllowed, ProfileUnsupported, RunRequest
from mesh.adapters.devin import DevinAdapter
from mesh.adapters.registry import get_adapter
from mesh.core.prompts import parse_deliverable, prompt_strategy

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNTH = os.path.join(ROOT, "tests", "fixtures", "synthetic", "devin")


def synth(name):
    with open(os.path.join(SYNTH, name), encoding="utf-8") as f:
        return f.read()


#: Profiles below unrestricted. None is mapped to a permission mode (readonly and edit are
#: unsupported), so build() raises ProfileUnsupported before its checks for them; the tests
#: run those checks through _Tmp.guard().
BELOW = ("readonly", "edit")

HELP = """Usage: devin [OPTIONS] [PATH]... [-- <PROMPT>...] [COMMAND]
      --prompt-file <FILE>
      --permission-mode <PERMISSION_MODE>
      --sandbox
      --cloud
      --model <MODEL>
  -p, --print [<PROMPT>]
      --respect-workspace-trust [<RESPECT_WORKSPACE_TRUST>]
"""


class _Tmp(unittest.TestCase):
    def tmpdir(self):
        d = tempfile.mkdtemp(prefix="mh-devin-")
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def req(self, profile="unrestricted", model="swe-2-flash-medium", effort=None, options=None, ws=None):
        d = self.tmpdir()
        prompt = os.path.join(d, "prompt.md")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("--not-a-flag: task text\n")
        workspace = ws or os.path.join(d, "ws")
        os.makedirs(workspace, exist_ok=True)
        return RunRequest(prompt_path=prompt, workspace=workspace, model=model, effort=effort,
                          profile=profile, options=dict(options or {}, run_id="run_x"))

    def guard(self, profile="readonly", ws=None):
        """The checks build() runs after the profile mapping, for a profile below unrestricted."""
        from mesh.adapters import devin as dv

        req = self.req(profile=profile, ws=ws)
        dv._refuse_env_wideners(req)
        dv._refuse_project_layer(req)

    def run_dir(self, stdout="", stderr=""):
        d = self.tmpdir()
        for name, text in (("stdout.log", stdout), ("stderr.log", stderr)):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(text)
        return d

    def stub(self, version="devin 3000.14.30821 (a09cd7f323ee)", version_rc=0, help_text=HELP,
             auth="auth_logged_in.txt"):
        d = self.tmpdir()
        for name, text in (("help.txt", help_text), ("auth.txt", synth(auth))):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(text)
        path = os.path.join(d, "devin")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n"
                    f'if [ "$1" = "--version" ]; then echo "{version}"; exit {version_rc}; fi\n'
                    f'if [ "$1" = "--help" ]; then cat "{d}/help.txt"; exit 0; fi\n'
                    f'if [ "$1" = "auth" ]; then cat "{d}/auth.txt"; exit 0; fi\n'
                    "exit 2\n")
        os.chmod(path, 0o755)
        return path


class TestDevinBuild(_Tmp):
    def test_unrestricted_argv_exact_and_prompt_not_in_argv(self):
        r = self.req()
        spec = DevinAdapter().build(r)
        self.assertEqual(spec.argv, [
            "devin", "-p", "--prompt-file", os.path.abspath(r.prompt_path), "--model", "swe-2-flash-medium",
            "--permission-mode", "dangerous", "--respect-workspace-trust", "false",
        ])
        self.assertEqual((spec.cwd, spec.stdin), (r.workspace, "devnull"))
        self.assertNotIn("--not-a-flag: task text\n", spec.argv)
        self.assertFalse(any("task text" in a for a in spec.argv))
        self.assertNotIn("--cloud", spec.argv)
        self.assertNotIn("--sandbox", spec.argv)

    def test_no_model_omits_flag(self):
        spec = DevinAdapter().build(self.req(model=None))
        self.assertNotIn("--model", spec.argv)

    def test_profile_mapping(self):
        a = DevinAdapter()
        self.assertEqual(a.profile_flags("unrestricted"), ["--permission-mode", "dangerous"])
        for profile in ("readonly", "edit", "dev", "elevated"):
            with self.subTest(profile=profile):
                with self.assertRaises(ProfileUnsupported):
                    a.profile_flags(profile)
                with self.assertRaises(ProfileUnsupported):
                    a.build(self.req(profile=profile))

    def test_readonly_is_unsupported_and_auto_is_never_emitted(self):
        # Review r2, measured 2026-11-19 (3000.14.30821, swe-2-flash, auto): the run read a
        # file in a workspace subdirectory, and in a repository outside the workspace, that
        # holds a skill with allowed-tools: [exec]; Devin loaded the skill and its shell
        # command ran. A scan before the launch cannot cover every readable directory, so
        # auto is not a readonly boundary and only unrestricted (dangerous) is mapped.
        from mesh.adapters import devin as dv

        self.assertEqual(dv.PERMISSION_MODES, {"unrestricted": "dangerous"})
        a = DevinAdapter()
        with self.assertRaises(ProfileUnsupported) as cm:
            a.profile_flags("readonly")
        self.assertEqual(cm.exception.details, {"adapter": "devin", "profile": "readonly"})
        with self.assertRaises(ProfileUnsupported) as cm:
            a.build(self.req(profile="readonly"))
        self.assertEqual(cm.exception.details, {"adapter": "devin", "profile": "readonly"})
        for profile in dv.PERMISSION_MODES:
            self.assertNotIn("auto", a.build(self.req(profile=profile)).argv)

    def test_edit_is_unsupported_and_accept_edits_is_never_emitted(self):
        # Review r2 P2-2, measured 2026-11-19 (3000.14.30821, swe-2-flash, accept-edits): the
        # run wrote .devin/skills/p/SKILL.md with allowed-tools: [exec], invoked the skill in
        # the same session and ran a shell command, so accept-edits is not an edit boundary.
        from mesh.adapters import devin as dv

        self.assertEqual(dv.PERMISSION_MODES, {"unrestricted": "dangerous"})
        with self.assertRaises(ProfileUnsupported) as cm:
            DevinAdapter().build(self.req(profile="edit"))
        self.assertEqual(cm.exception.details, {"adapter": "devin", "profile": "edit"})
        for profile in dv.PERMISSION_MODES:
            self.assertNotIn("accept-edits", DevinAdapter().build(self.req(profile=profile)).argv)

    def test_docs_profile_table_matches_the_adapter(self):
        from mesh.adapters import devin as dv

        with open(os.path.join(ROOT, "docs", "adapters-devin.md"), encoding="utf-8") as f:
            rows = {}
            for line in f:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) == 2 and cells[0].strip("`") in ("readonly", "edit", "dev", "elevated", "unrestricted"):
                    rows[cells[0].strip("`")] = cells[1]
        self.assertEqual(sorted(rows), ["dev", "edit", "elevated", "readonly", "unrestricted"])
        for profile, cell in rows.items():
            with self.subTest(profile=profile):
                mode = dv.PERMISSION_MODES.get(profile)
                if mode is None:
                    self.assertTrue(cell.startswith("unsupported"), cell)
                else:
                    self.assertTrue(cell.startswith(f"`--permission-mode {mode}`"), cell)

    def test_env_wideners_fail_closed_below_unrestricted(self):
        for name in ("DEVIN_SANDBOX", "DEVIN_CLI_DANGEROUSLY_FOLLOW_SYMLINKS"):
            with mock.patch.dict(os.environ, {name: "1"}):
                for profile in BELOW:
                    with self.subTest(name=name, profile=profile):
                        with self.assertRaises(AdapterUnsupported) as cm:
                            self.guard(profile)
                        self.assertEqual(cm.exception.details.get("reason"), "devin_env_widens_profile")
                        self.assertTrue(cm.exception.message.startswith("devin_env_widens_profile"))
                DevinAdapter().build(self.req(profile="unrestricted"))
        with mock.patch.dict(os.environ, {"DEVIN_SANDBOX": ""}):
            self.guard()

    def test_any_non_empty_env_widener_value_counts_as_set(self):
        # Smoke 2026-11-19: the CLI accepts DEVIN_SANDBOX=true (and rejects 1); both must refuse.
        for value in ("true", "1", "false", "yes", " "):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"DEVIN_SANDBOX": value}):
                with self.assertRaises(AdapterUnsupported) as cm:
                    self.guard("readonly")
                self.assertEqual(cm.exception.details.get("reason"), "devin_env_widens_profile")

    def test_effort_is_refused(self):
        with self.assertRaises(EffortNotAllowed) as cm:
            DevinAdapter().build(self.req(effort="high"))
        self.assertEqual(cm.exception.details["reason"], "effort_not_supported_by_cli")
        self.assertEqual(cm.exception.details["allowed"], [])

    def test_effort_is_refused_upstream_as_unsupported(self):
        from mesh.adapters.base import EffortUnsupported
        from mesh.core.policy import validate_resolved_effort

        with self.assertRaises(EffortUnsupported):
            validate_resolved_effort(adapter_id="devin", resolved_model="swe-2-flash", resolved_effort="high",
                                     allowed_efforts=["high"])

    def test_binary_option(self):
        stub = self.stub()
        spec = DevinAdapter().build(self.req(options={"binary": stub}))
        self.assertEqual(spec.argv[0], stub)

    def test_prompt_strategy_is_file(self):
        self.assertEqual(prompt_strategy("devin", "unrestricted", 10 ** 9), "file")

    def test_launch_is_held_until_m5(self):
        from mesh.adapters.evidence import LAUNCH_HOLDS, effective_launch

        self.assertEqual(LAUNCH_HOLDS.get("devin"), "M5")
        eff = effective_launch(None, DevinAdapter(), testing_mode=False)
        self.assertFalse(eff.ok)
        self.assertEqual(eff.reason, "launch_held")

    def test_docs_name_the_hold(self):
        # Review r2 P3-1: the docs said no adapter is held while devin is.
        from mesh.adapters import evidence

        def text(*rel):
            with open(os.path.join(ROOT, *rel), encoding="utf-8") as f:
                return " ".join(f.read().split())

        adapters_md = text("docs", "adapters.md")
        self.assertNotIn("No adapter has an explicit hold", adapters_md)
        self.assertIn("`devin` is on an explicit hold until M5", adapters_md)
        self.assertNotIn("table is empty again", " ".join((evidence.__doc__ or "").split()))
        self.assertIn("devin is held until M5", " ".join((evidence.__doc__ or "").split()))
        self.assertIn("devin is on a launch hold", text("README.md"))

    def test_registered(self):
        self.assertIsInstance(get_adapter("devin", testing_mode=False), DevinAdapter)


class TestDevinProjectLayer(_Tmp):
    def _ws_with(self, rel):
        ws = self.tmpdir()
        with open(os.path.join(ws, ".git"), "w") as f:
            f.write("gitdir: /elsewhere\n")
        path = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{}\n")
        return ws

    def test_layers_refused_below_unrestricted(self):
        for rel in (".devin/config.json", ".devin/config.local.json", ".devin/hooks.v1.json", ".mcp.json",
                    ".claude/settings.json", ".cursor/mcp.json", ".codex/config.toml",
                    ".devin/mcp_config.json", ".devin/mcp_config.local.json", ".devin/hooks.json",
                    "opencode.json", ".zed/settings.json"):
            ws = self._ws_with(rel)
            for profile in BELOW:
                with self.subTest(rel=rel, profile=profile):
                    with self.assertRaises(AdapterUnsupported) as cm:
                        self.guard(profile, ws)
                    self.assertTrue(cm.exception.message.startswith("devin_project_layer_present"))
                    self.assertEqual(cm.exception.details.get("reason"), "devin_project_layer_present")
            DevinAdapter().build(self.req(profile="unrestricted", ws=ws))

    def test_build_runs_the_checks_for_a_profile_mapped_below_unrestricted(self):
        # No profile below unrestricted is mapped, so build() stops at ProfileUnsupported.
        # A mapping that comes back gets the env and project layer checks without more code.
        from mesh.adapters import devin as dv

        ws = self._ws_with(".mcp.json")
        with self.assertRaises(ProfileUnsupported):
            DevinAdapter().build(self.req(profile="readonly", ws=ws))
        with mock.patch.dict(dv.PERMISSION_MODES, {"readonly": "auto"}):
            with self.assertRaises(AdapterUnsupported) as cm:
                DevinAdapter().build(self.req(profile="readonly", ws=ws))
            self.assertEqual(cm.exception.details.get("reason"), "devin_project_layer_present")
            with mock.patch.dict(os.environ, {"DEVIN_SANDBOX": "true"}):
                with self.assertRaises(AdapterUnsupported) as cm:
                    DevinAdapter().build(self.req(profile="readonly"))
            self.assertEqual(cm.exception.details.get("reason"), "devin_env_widens_profile")
        self.assertEqual(dv.PERMISSION_MODES, {"unrestricted": "dangerous"})

    #: Review r2 P3-2: every project layer entry, as literals. Dropping an entry from
    #: PROJECT_LAYERS fails test_every_project_layer_entry_is_refused.
    LAYERS = {
        ".devin": ("config.json", "config.local.json", "hooks.v1.json", "hooks.json", "mcp_config.json",
                   "mcp_config.local.json", "plugins", "agents"),
        ".cognition": ("config.json", "config.local.json", "hooks.v1.json", "hooks.json", "mcp_config.json",
                       "mcp_config.local.json", "plugins", "agents"),
        "": (".mcp.json", "opencode.json"),
        ".claude": ("settings.json", "settings.local.json"),
        ".cursor": ("mcp.json",),
        ".codex": ("config.toml",),
        ".gemini": ("settings.json",),
        ".windsurf": ("mcp_config.json", "hooks.json"),
        ".agents": ("plugins", "hooks.json", "agents"),
        ".zed": ("settings.json",),
    }

    def test_every_project_layer_entry_is_refused(self):
        from mesh.adapters import devin as dv

        self.assertEqual({layer[0]: tuple(layer[1]) for layer in dv.PROJECT_LAYERS}, self.LAYERS)
        self.assertEqual(len(dv.PROJECT_LAYERS), len(self.LAYERS))
        for config_dir, names in self.LAYERS.items():
            for name in names:
                rel = os.path.join(config_dir, name)
                ws = self._ws_with(rel)
                for profile in BELOW:
                    with self.subTest(rel=rel, profile=profile):
                        with self.assertRaises(AdapterUnsupported) as cm:
                            self.guard(profile, ws)
                        self.assertEqual(cm.exception.details.get("reason"), "devin_project_layer_present")
                        self.assertIn(rel, cm.exception.message)
                DevinAdapter().build(self.req(profile="unrestricted", ws=ws))

    SKILL_DIRS = (".devin", ".cognition", ".agents", ".claude", ".cursor", ".codex", ".kiro", ".github", ".windsurf",
                  ".codeium")

    def test_skill_dirs_are_literal(self):
        from mesh.adapters import devin as dv

        self.assertEqual(dv.SKILL_DIRS, self.SKILL_DIRS)

    def test_user_directories_are_user_config_not_a_project_layer(self):
        # A workspace that is the user's home: ~/.devin, ~/.claude, ... are the user's own
        # config and are skipped; files Devin has no user directory for are still refused.
        grant = "---\nname: s\nallowed-tools: [exec]\n---\nbody\n"
        rels = [os.path.join(d, n) for d, names in self.LAYERS.items() if d for n in names]
        rels += [os.path.join(d, "skills/s/SKILL.md") for d in self.SKILL_DIRS]
        for rel in rels:
            home = self._ws_with(rel)
            if rel.endswith("SKILL.md"):
                with open(os.path.join(home, rel), "w") as f:
                    f.write(grant)
            with self.subTest(rel=rel), mock.patch.dict(os.environ, {"HOME": home}):
                for profile in BELOW:
                    self.guard(profile, home)
            with self.subTest(rel=rel, home="elsewhere"):
                with self.assertRaises(AdapterUnsupported):
                    self.guard(ws=home)
        for rel in (".mcp.json", "opencode.json"):
            home = self._ws_with(rel)
            with self.subTest(rel=rel), mock.patch.dict(os.environ, {"HOME": home}):
                with self.assertRaises(AdapterUnsupported):
                    self.guard(ws=home)

    def test_refusal_message_is_bounded(self):
        # Review r2 P3-3: path names are task-controlled and the message lands in the
        # spawn_failed event of every attempt.
        ws = self._ws_with("AGENTS.md")
        for i in range(40):
            d = os.path.join(ws, ".devin", "skills", f"{i:02d}-" + "n" * 240)
            os.makedirs(d)
            with open(os.path.join(d, "SKILL.md"), "w") as f:
                f.write("---\nallowed-tools: [exec]\n---\n")
        with self.assertRaises(AdapterUnsupported) as cm:
            self.guard(ws=ws)
        msg = cm.exception.message
        self.assertTrue(msg.startswith("devin_project_layer_present"))
        self.assertLess(len(msg), 10 * 203 + 400)
        self.assertIn(" (+30 more)", msg)
        self.assertIn(".devin/skills/00-" + "n" * (200 - len(".devin/skills/00-")) + "..., ", msg)
        self.assertNotIn("n" * 201, msg)
        self.assertNotIn("skills/10-", msg)
        ws = self._ws_with(".mcp.json")
        with self.assertRaises(AdapterUnsupported) as cm:
            self.guard(ws=ws)
        self.assertIn("(.mcp.json)", cm.exception.message)

    def test_jj_root_layer_above_workspace_is_refused(self):
        root = self.tmpdir()
        os.makedirs(os.path.join(root, ".jj"))
        os.makedirs(os.path.join(root, ".devin"))
        with open(os.path.join(root, ".devin", "config.json"), "w") as f:
            f.write("{}\n")
        ws = os.path.join(root, "sub", "ws")
        os.makedirs(ws)
        with self.assertRaises(AdapterUnsupported):
            self.guard(ws=ws)

    def test_legacy_cognition_dir_is_refused(self):
        # Review r2 P1-1: Devin still reads the legacy project directory .cognition/ (MCP
        # servers, config, hooks, skills), so it is an alias of .devin and refused whole.
        from mesh.adapters import devin as dv

        self.assertIn(".cognition", dv.SKILL_DIRS)
        by_dir = {layer[0]: layer[1] for layer in dv.PROJECT_LAYERS}
        self.assertEqual(by_dir[".cognition"], by_dir[".devin"])
        for rel in (".cognition/mcp_config.json", ".cognition/mcp_config.local.json", ".cognition/config.json",
                    ".cognition/config.local.json", ".cognition/hooks.v1.json", ".cognition/hooks.json",
                    ".cognition/plugins/x", ".cognition/skills/s/SKILL.md", ".cognition/rules/style.md"):
            ws = self._ws_with(rel)
            for profile in BELOW:
                with self.subTest(rel=rel, profile=profile):
                    with self.assertRaises(AdapterUnsupported) as cm:
                        self.guard(profile, ws)
                    self.assertEqual(cm.exception.details.get("reason"), "devin_project_layer_present")
                    self.assertIn(".cognition", cm.exception.message)
            DevinAdapter().build(self.req(profile="unrestricted", ws=ws))

    def test_cognition_skill_grant_and_empty_dir_are_refused(self):
        ws = self._ws_with("AGENTS.md")
        os.makedirs(os.path.join(ws, ".cognition", "skills", "s"))
        with open(os.path.join(ws, ".cognition", "skills", "s", "SKILL.md"), "w") as f:
            f.write("---\nname: s\nallowed-tools: [exec]\n---\nbody\n")
        with self.assertRaises(AdapterUnsupported) as cm:
            self.guard(ws=ws)
        self.assertIn(".cognition", cm.exception.message)
        ws = self._ws_with("AGENTS.md")
        os.makedirs(os.path.join(ws, ".cognition"))
        with self.assertRaises(AdapterUnsupported):
            self.guard(ws=ws)

    def test_cognition_dir_above_workspace_up_to_root_is_refused(self):
        root = self.tmpdir()
        os.makedirs(os.path.join(root, ".jj"))
        os.makedirs(os.path.join(root, ".cognition"))
        ws = os.path.join(root, "sub", "ws")
        os.makedirs(ws)
        with self.assertRaises(AdapterUnsupported):
            self.guard(ws=ws)

    def _skill(self, rel, text):
        ws = self._ws_with("AGENTS.md")
        path = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return ws

    def _refused(self, ws):
        for profile in BELOW:
            with self.subTest(profile=profile):
                with self.assertRaises(AdapterUnsupported) as cm:
                    self.guard(profile, ws)
                self.assertEqual(cm.exception.details.get("reason"), "devin_project_layer_present")
        DevinAdapter().build(self.req(profile="unrestricted", ws=ws))

    def test_yaml_spellings_of_a_grant_are_refused(self):
        # Review r2 P1-2: each of these is shown as "Allowed tools: exec" by the real
        # `devin skills show` (3000.14.30821), or is a YAML form Mesh does not parse.
        cases = {
            "double-quoted key": '"allowed-tools": [exec]',
            "single-quoted key": "'permissions':\n  allow: ['exec']",
            "indented mapping": "  name: x\n  allowed-tools: [exec]",
            "flow mapping": "{name: x, allowed-tools: [exec]}",
            "complex key": "? allowed-tools\n: [exec]",
            "tagged key": "!!str allowed-tools: [exec]",
            "anchored key": "&a allowed-tools: [exec]",
            "CR line break": "name: x\rallowed-tools: [exec]",
            "NEL line break": "name: x\u0085allowed-tools: [exec]",
            "LS line break": "name: x\u2028allowed-tools: [exec]",
            "tab after colon": "name: x\nallowed-tools:\t[exec]",
            "string grant": "name: x\nallowed-tools: read, exec",
            "merge key": "name: x\n<<: {allowed-tools: [exec]}",
        }
        for name, fm in cases.items():
            with self.subTest(case=name):
                self._refused(self._skill(".devin/skills/s/SKILL.md", f"---\n{fm}\n---\nbody\n"))

    def test_frontmatter_markers_devin_accepts_are_refused(self):
        # Real CLI: leading blank lines or indentation, "--- " and CRLF all still open a
        # frontmatter, and an indented " ---" does not close it.
        grant = "allowed-tools: [exec]"
        cases = {
            "leading blank lines": f"\n\n---\nname: x\n{grant}\n---\nbody\n",
            "indented opener": f"  ---\nname: x\n{grant}\n---\nbody\n",
            "tab before opener": f"\t---\nname: x\n{grant}\n---\nbody\n",
            "opener trailing space": f"--- \nname: x\n{grant}\n---\nbody\n",
            "opener with comment": f"--- # c\nname: x\n{grant}\n---\nbody\n",
            "CRLF": f"---\r\nname: x\r\n{grant}\r\n---\r\nbody\r\n",
            "CRLF without grant": "---\r\nname: x\r\n---\r\nbody\r\n",
            "indented closer": f"---\nname: x\n ---\n{grant}\n---\nbody\n",
            "closer at EOF": f"---\nname: x\n{grant}\n---",
            "BOM": f"\ufeff---\nname: x\n{grant}\n---\nbody\n",
            "CR before a fake closer": f"---\nname: x\r---\r{grant}\n---\nbody\n",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self._refused(self._skill(".devin/skills/s/SKILL.md", text))

    def test_frontmatter_that_is_not_plain_key_value_lines_is_refused(self):
        # Fail closed: only column-0 bare keys from a short list, one line each, pass.
        for name, fm in (("unknown key", "name: x\ntools: [exec]"),
                         ("key case", "name: x\nAllowed-Tools: [exec]"),
                         ("comment", "# note\nname: x"),
                         ("block list", "name: x\ntriggers:\n  - user"),
                         ("block scalar", "name: x\ndescription: |\n  text"),
                         ("nested map", "name: x\nmetadata:\n  a: b"),
                         ("no space after colon", "name:x"),
                         ("open quote", 'name: "x'),
                         ("escaped quote", 'name: "x\\"y"'),
                         ("alias value", "name: *a"),
                         ("flow map value", "description: {a: b}"),
                         ("whitespace-only line", "name: x\n  \ndescription: d"),
                         ("vertical tab", "name: x\x0bdescription: d")):
            with self.subTest(case=name):
                self._refused(self._skill(".devin/skills/s/SKILL.md", f"---\n{fm}\n---\nbody\n"))

    def test_plain_instruction_skills_still_pass(self):
        for name, text in (
            ("documented plain keys", '---\nname: a\ndescription: Use when asked to "review": d\n'
                                      'argument-hint: "[file] [options]"\ntriggers: [user, model]\n'
                                      "license: MIT\ncompatibility: 'any'\n---\nallowed-tools: in body\n"),
            ("blank line and empty value", "---\nname: a\n\ndescription:\n---\nbody\n"),
            ("no frontmatter", "# Title\n\ntext\n\n---\n\nallowed-tools: [exec]\n"),
            ("empty file", ""),
        ):
            with self.subTest(case=name):
                ws = self._skill(".devin/skills/a/SKILL.md", text)
                for profile in BELOW:
                    self.guard(profile, ws)

    def test_skill_keys_that_change_model_or_agent_are_refused(self):
        # Review r2 P3-4 (vendor docs, creating-skills.mdx): `model:` overrides the run's
        # model and `agent:` / `subagent:` run the skill under another profile, so a
        # task-controlled skill would step outside Mesh's --allowed-model list.
        for fm in ("model: opus", "agent: reviewer", "subagent: true"):
            with self.subTest(fm=fm):
                self._refused(self._skill(".devin/skills/s/SKILL.md", f"---\nname: s\n{fm}\n---\nbody\n"))

    def test_project_subagent_profiles_are_refused(self):
        # subagents.mdx: agents/<name>.md and agents/<name>/AGENT.md under .devin and .agents
        # define subagent profiles with their own `model:`.
        for rel in (".devin/agents/reviewer.md", ".devin/agents/researcher/AGENT.md", ".agents/agents/reviewer.md",
                    ".cognition/agents/reviewer.md"):
            with self.subTest(rel=rel):
                self._refused(self._ws_with(rel))

    def test_any_symlink_in_a_skills_tree_is_refused(self):
        # Devin follows symlinks in skills/ (real CLI: a symlinked skill directory is loaded
        # with its grants); os.walk does not, so Mesh cannot vouch for the target.
        grant = "---\nname: r\nallowed-tools: [exec]\n---\nbody\n"
        plain = "---\nname: r\n---\nbody\n"
        ws = self._skill("docs/real/SKILL.md", grant)
        os.makedirs(os.path.join(ws, ".devin", "skills"))
        os.symlink("../../docs/real", os.path.join(ws, ".devin", "skills", "sym"))
        self._refused(ws)
        ws = self._skill("docs/real/SKILL.md", plain)
        os.makedirs(os.path.join(ws, ".devin", "skills", "s"))
        os.symlink("../../../docs/real/SKILL.md", os.path.join(ws, ".devin", "skills", "s", "SKILL.md"))
        self._refused(ws)
        ws = self._skill("docs/skills/real/SKILL.md", grant)
        os.makedirs(os.path.join(ws, ".claude"))
        os.symlink("../docs/skills", os.path.join(ws, ".claude", "skills"))
        self._refused(ws)
        ws = self._skill(".agents/skills/s/SKILL.md", plain)
        os.symlink("nowhere", os.path.join(ws, ".agents", "skills", "s", "notes.md"))
        self._refused(ws)

    def test_skill_file_name_is_matched_case_insensitively(self):
        for fname in ("skill.md", "Skill.MD", "SKILL.MD"):
            with self.subTest(fname=fname):
                self._refused(self._skill(f".devin/skills/s/{fname}",
                                          "---\nname: s\nallowed-tools: [exec]\n---\nbody\n"))

    def test_frontmatter_past_the_scan_head_counts_as_grant(self):
        from mesh.adapters import devin as dv

        text = "---\nname: a\n----\nallowed-tools: [exec]\n---\nbody\n"
        ws = self._skill(".devin/skills/s/SKILL.md", text)
        with mock.patch.object(dv, "SKILL_HEAD_BYTES", len("---\nname: a\n---")):
            self._refused(ws)

    def test_skill_with_grants_is_refused(self):
        for sd in (".devin", ".agents", ".claude", ".cursor", ".codex", ".kiro", ".github", ".windsurf", ".codeium"):
            for fm in ("allowed-tools: [exec]", "permissions:\n  allow: [\"Exec(sh)\"]"):
                ws = self._skill(f"{sd}/skills/build/deep/SKILL.md", f"---\nname: b\n{fm}\n---\nbody\n")
                with self.subTest(sd=sd, fm=fm):
                    with self.assertRaises(AdapterUnsupported) as cm:
                        self.guard(ws=ws)
                    self.assertEqual(cm.exception.details.get("reason"), "devin_project_layer_present")
                    DevinAdapter().build(self.req(profile="unrestricted", ws=ws))

    def test_skill_without_grants_and_grant_word_in_body_pass(self):
        ws = self._skill(".devin/skills/a/SKILL.md", "---\nname: a\ndescription: d\n---\npermissions: in body\n")
        self.guard(ws=ws)

    def test_unreadable_unterminated_or_too_many_skills_count_as_grants(self):
        from mesh.adapters import devin as dv

        ws = self._skill(".devin/skills/a/SKILL.md", "---\nname: a\n")
        with self.assertRaises(AdapterUnsupported):
            self.guard(ws=ws)
        ws = self._skill(".devin/skills/a/SKILL.md", "---\nname: a\n---\n")
        os.chmod(os.path.join(ws, ".devin/skills/a/SKILL.md"), 0)
        if os.geteuid() != 0:
            with self.assertRaises(AdapterUnsupported):
                self.guard(ws=ws)
        ws = self._skill(".devin/skills/a/SKILL.md", "---\nname: a\n---\n")
        with mock.patch.object(dv, "SKILL_SCAN_MAX_ENTRIES", 1):
            with self.assertRaises(AdapterUnsupported):
                self.guard(ws=ws)

    def _returns_without_blocking(self, fn, fifo):
        """Run ``fn`` in a thread; if it blocks reading ``fifo``, release it and fail."""
        import threading

        out = {}

        def run():
            try:
                out["value"] = fn()
            except Exception as e:  # noqa: BLE001 - handed to the test thread
                out["error"] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(5)
        blocked = t.is_alive()
        if blocked:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            t.join(5)
        self.assertFalse(blocked, "blocked on a FIFO (would hang the meshd event loop)")
        return out

    def test_fifo_skill_file_is_refused_without_blocking(self):
        # Review r2 P2-1: build() runs on the scheduler's event loop; a plain open() of a
        # task-controlled FIFO blocked it, and read as empty (no grant) once a writer came.
        ws = self._ws_with("AGENTS.md")
        os.makedirs(os.path.join(ws, ".devin", "skills", "x"))
        fifo = os.path.join(ws, ".devin", "skills", "x", "SKILL.md")
        os.mkfifo(fifo)
        for profile in BELOW:
            with self.subTest(profile=profile):
                out = self._returns_without_blocking(lambda: self.guard(profile, ws), fifo)
                self.assertIsInstance(out.get("error"), AdapterUnsupported)
                self.assertEqual(out["error"].details.get("reason"), "devin_project_layer_present")

    def test_skill_file_read_does_not_follow_symlinks_or_non_regular_files(self):
        from mesh.adapters import devin as dv

        d = self.tmpdir()
        plain = os.path.join(d, "plain.md")
        with open(plain, "w") as f:
            f.write("---\nname: a\n---\nbody\n")
        self.assertFalse(dv._skill_grants(plain))
        link = os.path.join(d, "link.md")
        os.symlink(plain, link)
        self.assertTrue(dv._skill_grants(link))
        fifo = os.path.join(d, "fifo")
        os.mkfifo(fifo)
        fifo_link = os.path.join(d, "fifo-link.md")
        os.symlink(fifo, fifo_link)
        for path in (fifo, fifo_link):
            with self.subTest(path=os.path.basename(path)):
                out = self._returns_without_blocking(lambda p=path: dv._skill_grants(p), fifo)
                self.assertIs(out.get("value"), True)
        self.assertTrue(dv._skill_grants(d))
        self.assertTrue(dv._skill_grants(os.path.join(d, "missing.md")))
        self.assertTrue(dv._skill_grants("/dev/null"))

    def test_instruction_files_are_not_refused(self):
        for rel in ("AGENTS.md", ".devin/rules/style.md", ".devin/skills/x/SKILL.md", "CLAUDE.md",
                    ".claude/skills/y/SKILL.md"):
            with self.subTest(rel=rel):
                self.guard(ws=self._ws_with(rel))


class TestDevinParse(_Tmp):
    def test_ok_strips_escapes_and_finds_deliverable(self):
        res = DevinAdapter().parse(self.run_dir(stdout=synth("stdout_ok.txt")))
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertTrue(res.final_text.startswith("Done. Summary below."))
        self.assertNotIn("\x1b", res.final_text)
        self.assertEqual(res.usage, {"status": "completed"})
        self.assertEqual(parse_deliverable(res.final_text)["summary"], "ok")

    def test_failure_meanings(self):
        cases = (
            ("stderr_login_canceled.txt", "auth_failed"),
            ("stderr_unknown_model.txt", "run_error"),
            ("stderr_untrusted.txt", "run_error"),
        )
        for name, meaning in cases:
            with self.subTest(name=name):
                res = DevinAdapter().parse(self.run_dir(stderr=synth(name)))
                self.assertEqual(res.vendor_exit_meaning, meaning)
                self.assertEqual(res.usage["status"], meaning)
        for text, meaning in (("Error: Not logged in.\n", "auth_failed"),
                              ("Error: request failed: 401 Unauthorized\n", "auth_failed"),
                              ("Error: You have reached your usage limit\n", "quota_exhausted"),
                              ("Error: quota exceeded for this team\n", "quota_exhausted"),
                              ("Error: 429 Too Many Requests\n", "rate_limited"),
                              ("Error: rate limit reached, retry later\n", "rate_limited"),
                              ("Error: error sending request: connection refused\n", "network_error"),
                              ("Error: something broke\n", "run_error")):
            with self.subTest(text=text):
                self.assertEqual(DevinAdapter().parse(self.run_dir(stderr=text)).vendor_exit_meaning, meaning)

    def test_rejected_arguments_reach_supervisor_as_cli_usage_error(self):
        from mesh.daemon.supervisor import _vendor_nonzero_failure

        d = self.run_dir(stderr=synth("stderr_no_prompt.txt"))
        res = DevinAdapter().parse(d)
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertEqual(res.usage["status"], "cli_usage_error")
        self.assertEqual(_vendor_nonzero_failure(d, "devin", 1, testing_mode=False)[0], "cli_usage_error")
        d = self.run_dir(stderr=synth("stderr_login_canceled.txt"))
        self.assertEqual(_vendor_nonzero_failure(d, "devin", 1, testing_mode=False)[0], "auth_failed")

    def test_only_error_lines_are_classified(self):
        cases = (
            ("warning: MCP server x: connection refused\nError: Unknown model: 'nope'\n", "run_error", "unknown_model"),
            ("Error: Unknown model: 'quota-test'\n", "run_error", "unknown_model"),
            ("Error: Unknown model: \"rate-limit-429\"\n", "run_error", "unknown_model"),
            ("Error: context window limit exceeded\n", "run_error", "context_window"),
            ("warning: retrying after rate limit\nError: something broke\n", "run_error", None),
            ("Error: quota\n", "run_error", None),
        )
        for text, meaning, kind in cases:
            with self.subTest(text=text):
                self.assertEqual(DevinAdapter().parse(self.run_dir(stderr=text)).vendor_exit_meaning, meaning)
                parsed = DevinAdapter.parse_stderr_error(text)
                if kind:
                    self.assertEqual(parsed["details"]["error"], kind)
                else:
                    self.assertIsNone(parsed)

    def test_cached_meaning_follows_stderr_changes(self):
        from mesh.daemon.supervisor import vendor_failure_meaning

        d = self.run_dir(stdout="partial\n")
        self.assertIsNone(vendor_failure_meaning(d, "devin", testing_mode=False))
        with open(os.path.join(d, "stderr.log"), "w", encoding="utf-8") as f:
            f.write("Error: You have reached your usage limit\n")
        self.assertEqual(vendor_failure_meaning(d, "devin", testing_mode=False), "quota_exhausted")

    def test_exit_zero_with_error_line_is_a_meaning(self):
        # supervisor: exit 0 with a meaning is vendor_error (same rule as codex/claude).
        res = DevinAdapter().parse(self.run_dir(stdout="partial\n", stderr="Error: usage limit reached\n"))
        self.assertEqual(res.vendor_exit_meaning, "quota_exhausted")

    def test_warning_text_without_error_line_is_not_a_failure(self):
        res = DevinAdapter().parse(self.run_dir(stdout="done\n", stderr="retrying after rate limit\n"))
        self.assertIsNone(res.vendor_exit_meaning)

    def test_rejected_tool_is_tool_denied_not_failure(self):
        res = DevinAdapter().parse(self.run_dir(stdout="Both were rejected\n", stderr=synth("stderr_tool_rejected.txt")))
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertEqual(res.usage["tool_denied"], 2)
        self.assertEqual(DevinAdapter.parse_stderr_error(synth("stderr_tool_rejected.txt"))["reason"], "tool_denied")

    def test_model_reply_quoting_the_warning_is_not_counted(self):
        line = "warning: rejected a tool call that requires confirmation.\n"
        res = DevinAdapter().parse(self.run_dir(stdout=line * 2, stderr="note: rejected a tool call\n"))
        self.assertNotIn("tool_denied", res.usage)

    def test_missing_logs(self):
        res = DevinAdapter().parse(self.tmpdir())
        self.assertEqual((res.final_text, res.vendor_exit_meaning), ("", None))

    def test_parse_stderr_error(self):
        p = DevinAdapter.parse_stderr_error
        self.assertEqual(p(synth("stderr_untrusted.txt"))["details"]["error"], "workspace_untrusted")
        self.assertEqual(p(synth("stderr_unknown_model.txt"))["details"]["error"], "unknown_model")
        self.assertEqual(p(synth("stderr_no_prompt.txt"))["reason"], "cli_usage_error")
        self.assertEqual(p(synth("stderr_login_canceled.txt"))["reason"], "auth_failed")
        self.assertIsNone(p(""))
        self.assertIsNone(p("some log line\n"))


class TestDevinAuthAndProbe(_Tmp):
    def test_auth_status_text_decides_not_rc(self):
        self.assertEqual(DevinAdapter.parse_auth_status(0, synth("auth_not_logged_in.txt")),
                         {"method": "none", "logged_in": False, "exit_code": 0})
        self.assertEqual(DevinAdapter.parse_auth_status(0, synth("auth_logged_in.txt")),
                         {"method": "subscription", "logged_in": True, "exit_code": 0})
        self.assertEqual(DevinAdapter.parse_auth_status(0, "")["method"], "unknown")
        self.assertFalse(DevinAdapter.parse_auth_status(0, "something new\n")["logged_in"])

    def test_probe_ok_and_auth_without_personal_fields(self):
        p = DevinAdapter().probe(binary_path=self.stub())
        self.assertEqual((p.status, p.version), ("partially_verified", "3000.14.30821"))
        self.assertIsNone(p.flag_diff)
        self.assertEqual(p.details["auth"], {"method": "subscription", "logged_in": True, "exit_code": 0})
        dumped = json.dumps(p.to_dict())
        self.assertNotIn("jane", dumped.lower())
        self.assertNotIn("credentials.toml", dumped)
        self.assertEqual(p.capabilities["headless"]["verification"], "help")
        self.assertEqual(p.capabilities["reasoning_effort"]["supported"], "no")

    def test_probe_not_logged_in(self):
        p = DevinAdapter().probe(binary_path=self.stub(auth="auth_not_logged_in.txt"))
        self.assertEqual(p.details["auth"]["method"], "none")

    def test_missing_flag_is_reported(self):
        p = DevinAdapter().probe(binary_path=self.stub(help_text=HELP.replace("      --prompt-file <FILE>\n", "")))
        self.assertEqual(p.flag_diff, {"match": False, "missing_flags": ["--prompt-file"]})

    def test_version_failures(self):
        self.assertEqual(DevinAdapter().probe(binary_path=self.stub(version_rc=2)).status, "error")
        self.assertEqual(DevinAdapter().probe(binary_path=self.stub(version="devin dev")).status, "unverified")
        p = DevinAdapter().probe(binary_path=self.stub(version="devin 3000.15.1 (abc)"))
        self.assertIn("3000.14.30821", p.details["version_note"])

    def test_not_installed_is_clear_not_a_crash(self):
        p = DevinAdapter().probe(binary_path=os.path.join(self.tmpdir(), "missing-devin"))
        self.assertEqual(p.status, "not_installed")
        self.assertIsNone(p.version)
        self.assertIn("curl -fsSL https://cli.devin.ai/install.sh | bash", p.details["hint"])

    def test_tripwire_shim_on_path_counts_as_not_installed(self):
        root = self.tmpdir()
        shims = os.path.join(root, "tests", "shims")
        other = os.path.join(root, "bin")
        os.makedirs(shims)
        os.makedirs(other)
        for d in (shims, other):
            with open(os.path.join(d, "devin"), "w") as f:
                f.write("#!/bin/sh\nexit 99\n")
            os.chmod(os.path.join(d, "devin"), 0o755)
        with mock.patch.dict(os.environ, {"PATH": shims + os.pathsep + other}):
            self.assertIsNone(DevinAdapter().resolve_binary())
            self.assertEqual(DevinAdapter().probe().status, "not_installed")
        with mock.patch.dict(os.environ, {"PATH": other}):
            self.assertEqual(DevinAdapter().resolve_binary(), os.path.join(other, "devin"))

    def test_repo_shim_exists(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "tests", "shims", "devin")))

    def test_list_models_is_user_supplied(self):
        self.assertIsNone(DevinAdapter().list_models(self.stub()))


if __name__ == "__main__":
    unittest.main()
