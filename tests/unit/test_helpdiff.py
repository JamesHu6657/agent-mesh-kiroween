"""Unit tests for mesh/adapters/helpdiff.py (m3-r1; ACC: N10, N22). Offline, fixtures read-only."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from mesh.adapters import helpdiff
from mesh.adapters.base import RunRequest, is_flag_present
from mesh.adapters.registry import ADAPTER_CLASSES

VENDORS = ("droid", "agy", "kiro", "vibe")
HELP_FILES = {"droid": ("help.txt", "exec-help.txt"), "agy": ("help.txt",), "kiro": ("help.txt", "chat-help.txt"),
              "vibe": ("help.txt",)}


def _read(adapter_id: str, version: str, name: str) -> str:
    return (helpdiff.fixture_root() / adapter_id / version / name).read_text(encoding="utf-8")


class FlagTokensTest(unittest.TestCase):
    def test_token_rule_matches_the_probe(self) -> None:
        """ACC: N22 - flag tokens follow is_flag_present's boundaries (no --print inside --print-timeout)."""
        text = "Usage: x [--print] --print-timeout=5, -m <model>\n  --trust-tools=<t>  (--cwd)\n  word-with-dash --no\n"
        toks = helpdiff.flag_tokens(text)
        self.assertEqual(toks, {"--print", "--print-timeout", "-m", "--trust-tools", "--cwd", "--no"})
        for t in toks:
            self.assertTrue(is_flag_present(t, text), t)
        self.assertEqual(helpdiff.flag_tokens(""), set())

    def test_diff_added_and_removed_sorted(self) -> None:
        """ACC: N22 - diff reports added and removed flags, each sorted."""
        out = helpdiff.diff("  --b x\n  --a y\n  --gone\n", "  --a y\n  --b x\n  --z new\n  --c new\n")
        self.assertEqual(out, {"added": ["--c", "--z"], "removed": ["--gone"]})


class FixtureDiffTest(unittest.TestCase):
    def test_fixture_against_itself_is_empty(self) -> None:
        """ACC: N22 - the diff of each committed help fixture with itself is empty."""
        for a in VENDORS:
            d = helpdiff.newest_fixture_dir(a)
            self.assertIsNotNone(d, a)
            for name in HELP_FILES[a]:
                text = (d / name).read_text(encoding="utf-8")
                with self.subTest(adapter=a, file=name):
                    self.assertTrue(helpdiff.flag_tokens(text))
                    self.assertEqual(helpdiff.diff(text, text), {"added": [], "removed": []})

    def test_emitted_flags_in_planned_fixtures(self) -> None:
        """ACC: N10, N22 - every flag build() emits over the golden matrix is in the planned version's help."""
        for a in VENDORS:
            adapter = ADAPTER_CLASSES[a]()
            emitted = helpdiff.emitted_flags(adapter)
            with self.subTest(adapter=a):
                self.assertTrue(emitted)
                toks = set()
                for name in HELP_FILES[a]:
                    toks |= helpdiff.flag_tokens(_read(a, adapter.planned_version, name))
                self.assertEqual(sorted(emitted - toks), [])
        self.assertIn("--log-group-id", helpdiff.emitted_flags(ADAPTER_CLASSES["droid"]()))
        self.assertIn("--skip-permissions-unsafe", helpdiff.emitted_flags(ADAPTER_CLASSES["droid"]()))

    def test_newer_fixture_dirs_diff_printed(self) -> None:
        """ACC: N22 - for an adapter whose newest fixture dir is newer than planned_version, the diff is computed and printed (not asserted empty)."""
        printed = {}
        for a in VENDORS:
            adapter = ADAPTER_CLASSES[a]()
            newest = helpdiff.newest_fixture_dir(a)
            if newest is None or newest.name == adapter.planned_version:
                continue
            per_file = {}
            for name in HELP_FILES[a]:
                new_path = newest / name
                if new_path.is_file():
                    per_file[name] = helpdiff.diff(_read(a, adapter.planned_version, name),
                                                   new_path.read_text(encoding="utf-8"))
            printed[f"{a} {adapter.planned_version} -> {newest.name}"] = per_file
        print(json.dumps({"helpdiff_vs_planned": printed}, sort_keys=True))
        for diffs in printed.values():
            for d in diffs.values():
                self.assertEqual(set(d), {"added", "removed"})


class EmittedMatrixTest(unittest.TestCase):
    def test_model_option_and_long_prompt_flags_are_covered(self) -> None:
        """ACC: N10, N22 - the emitted-flag matrix covers model, adapter options and the long-prompt path (Kiro r1 MAJOR 1)."""
        em = {a: helpdiff.emitted_flags(ADAPTER_CLASSES[a]()) for a in ("vibe", "agy", "kiro")}
        self.assertTrue({"--model", "--trust", "--max-turns", "--max-price", "--max-tokens"} <= em["vibe"], em["vibe"])
        self.assertTrue({"--add-dir", "--sandbox", "--agent"} <= em["agy"], em["agy"])
        self.assertIn("--agent", em["kiro"])

    def test_golden_shape_argvs_are_subsets(self) -> None:
        """ACC: N10, N22 - flags of locally rebuilt golden-shape argvs (vibe and agy with a model, options, long prompt) are a subset of emitted_flags."""
        tmp = tempfile.mkdtemp(prefix="mh-hd-gold-")
        self.addCleanup(shutil.rmtree, tmp, True)
        short = os.path.join(tmp, "p.md")
        with open(short, "w", encoding="utf-8") as f:
            f.write("golden\n")
        cases = []
        vibe = ADAPTER_CLASSES["vibe"]()
        cases.append((vibe, RunRequest(prompt_path=short, workspace=tmp, model="devstral-2", effort=None, profile="dev",
                                       options={"run_id": "run_g1", "trust_workdir": True, "max_turns": 5,
                                                "max_price_usd": 2, "max_tokens": 1000})))
        agy = ADAPTER_CLASSES["agy"]()
        long_p = os.path.join(tmp, "long.md")
        with open(long_p, "w", encoding="utf-8") as f:
            f.write("y" * (agy.max_argv_prompt_bytes + 1))
        cases.append((agy, RunRequest(prompt_path=long_p, workspace=tmp, model="gemini-3.8-pro", effort="high",
                                      profile="readonly", options={"run_id": "run_g2", "sandbox": True,
                                                                   "agent": "reviewer"})))
        for adapter, req in cases:
            flags = {t.split("=", 1)[0] for t in adapter.build(req).argv[1:] if t.startswith("--")}
            with self.subTest(adapter=adapter.id):
                self.assertTrue(flags)
                self.assertEqual(sorted(flags - helpdiff.emitted_flags(adapter)), [])


class NewestFixtureDirTest(unittest.TestCase):
    def test_numeric_version_order(self) -> None:
        """ACC: N22 - newest_fixture_dir compares versions numerically and ignores non-version names."""
        root = Path(tempfile.mkdtemp(prefix="mh-hd-"))
        self.addCleanup(shutil.rmtree, str(root), True)
        self.assertTrue(str(root).startswith("/tmp/mh-"))
        for v in ("1.3.0", "1.9.2", "1.10.0", "notes", "1.10"):
            (root / "droid" / v).mkdir(parents=True)
        (root / "droid" / "2.0.0").write_text("a file, not a dir\n")
        self.assertEqual(helpdiff.newest_fixture_dir("droid", root).name, "1.10.0")
        self.assertIsNone(helpdiff.newest_fixture_dir("agy", root))
        (root / "agy").mkdir()
        self.assertIsNone(helpdiff.newest_fixture_dir("agy", root))
        self.assertEqual(helpdiff.newest_fixture_dir("droid").parent.name, "droid")


if __name__ == "__main__":
    unittest.main()
