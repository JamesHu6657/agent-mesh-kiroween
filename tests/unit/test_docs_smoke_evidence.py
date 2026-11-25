"""Docs follow the committed smoke evidence of codex, claude, devin, agy and vibe (PR #60 review).

ACC: N12, N19

Offline only: reads README.md, docs/adapters*.md, mesh/adapters/evidence.py and the committed
results under tests/smoke/results/. Three rules, each from a review finding:

* a page about an adapter that has a committed results file names that file and does not say
  that no real run or results file exists;
* the launch column of the README status table equals what ``evidence.load_evidence`` and the
  launch hold say for the committed results at their own version;
* a README status row says "denied" only when the adapter's shell case recorded a denial, and
  the numbers the smoke-tier pages quote are the recorded ones.
"""

import json
import os
import re
import unittest

from mesh.adapters import evidence, smoke_schema

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULTS_DIR = os.path.join(ROOT, "tests", "smoke", "results")

SMOKE_TIER_ADAPTERS = ("codex", "claude", "devin")
#: page -> adapters whose committed results the page must name.
PAGES = {
    "docs/adapters.md": SMOKE_TIER_ADAPTERS + ("agy",),
    "docs/adapters-codex-claude.md": ("codex", "claude"),
    "docs/adapters-devin.md": ("devin",),
}
#: Statements that are false once a results file is committed (normalized, lower case).
STALE_PHRASES = (
    "no real run",
    "no results file",
    "no real fixture",
    "no real results are committed",
    "no real smoke evidence",
    "no smoke evidence yet",
    "or smoke evidence yet",
    "no promoted results",
    "not committed yet",
    "first real run",
)
#: Counters a shell case (S-<ADAPTER>-8) records when the vendor refused or failed a tool call.
DENIAL_COUNTERS = ("permission_denials", "failed_commands", "denied_effects", "failed_effects",
                   "denied_lines", "tool_errors", "denied_actions", "tool_rejections")
#: README status rows: adapter id -> the strict launch sentence lists it.
README_STRICT_SENTENCE = "In strict mode droid, kiro, codex and claude launch."


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def normalize(text):
    return " ".join(text.split())


def latest(adapter):
    """``(repo-relative path, doc)`` of the adapter's latest committed results file."""
    path = smoke_schema.load_latest(RESULTS_DIR, adapter)
    if path is None:
        raise AssertionError(f"no committed results file for {adapter}")
    with open(path, encoding="utf-8") as handle:
        return os.path.relpath(path, ROOT).replace(os.sep, "/"), json.load(handle)


def case(doc, case_id):
    return next(c for c in doc["cases"] if c["id"] == case_id)


def shell_case(doc):
    return case(doc, f"S-{doc['adapter'].upper()}-8")


def table_row(text, first_cell_re):
    for line in text.splitlines():
        if line.startswith("|") and re.match(first_cell_re, line.strip().strip("|").strip()):
            return line
    raise AssertionError(f"no table row matching {first_cell_re!r}")


def readme_row(adapter):
    return table_row(read("README.md"), rf".*\(`{adapter}`\)")


class TestDocsSmokeEvidence(unittest.TestCase):
    """The smoke-tier pages and the README agree with the committed results files."""

    def test_pages_name_the_committed_results_and_do_not_deny_them(self):
        """ACC: N12 - each page about codex, claude or devin names that adapter's latest committed results file (docs/adapters.md also names agy's, the 1.5.12 rerun) and has none of the "no real run" / "no results file" statements; the LAUNCH_HOLDS comment in evidence.py has none either."""
        for page, adapters in PAGES.items():
            text = normalize(read(page))
            for adapter in adapters:
                with self.subTest(page=page, adapter=adapter):
                    rel, _ = latest(adapter)
                    self.assertIn(rel, text)
            for phrase in STALE_PHRASES:
                with self.subTest(page=page, phrase=phrase):
                    self.assertNotIn(phrase, text.lower())
        source = read("mesh/adapters/evidence.py")
        comment = normalize(source[source.index("#: L11:"):source.index("LAUNCH_HOLDS: Dict")].replace("#:", " "))
        for phrase in STALE_PHRASES:
            with self.subTest(page="mesh/adapters/evidence.py", phrase=phrase):
                self.assertNotIn(phrase, comment.lower())

    def test_readme_launch_column_matches_the_evidence_gate(self):
        """ACC: N19 - with the committed results at their own version, strict launch is on for droid, kiro, codex and claude only; devin's evidence passes and the M5 hold still refuses it; agy and vibe are launch_cases_failed, each with S-X-1 pass and S-X-6 fail (agy 1.5.12 is the rerun with the prompt directly after --print). The README launch column and its strict-mode sentence say the same."""
        strict = {}
        for adapter in ("droid", "kiro", "codex", "claude", "agy", "vibe", "devin"):
            _, doc = latest(adapter)
            raw = evidence.load_evidence(adapter, False, doc["version"], env={})
            self.assertEqual(raw.errors, [], adapter)
            passed = raw.ok
            ev = evidence.apply_hold(raw, adapter, False, env={})
            strict[adapter] = (passed, ev.ok, ev.reason, ev.milestone)
        self.assertEqual(sorted(a for a, v in strict.items() if v[1]), ["claude", "codex", "droid", "kiro"])
        self.assertEqual(strict["devin"], (True, False, "launch_held", "M5"))
        self.assertEqual(evidence.LAUNCH_HOLDS, {"devin": "M5"})
        for adapter in ("agy", "vibe"):
            self.assertEqual(strict[adapter], (False, False, "launch_cases_failed", None), adapter)
            _, doc = latest(adapter)
            self.assertEqual([case(doc, cid)["status"] for cid in smoke_schema.LAUNCH_CASES], ["pass", "fail"],
                             adapter)

        readme = read("README.md")
        self.assertIn(README_STRICT_SENTENCE, normalize(readme))
        for adapter, (_, ok, reason, _) in strict.items():
            with self.subTest(adapter=adapter):
                _, doc = latest(adapter)
                row = [c.strip() for c in readme_row(adapter).strip().strip("|").split("|")]
                self.assertEqual(row[1], doc["version"])
                launch = row[-1]
                if ok:
                    self.assertEqual(launch, "yes")
                elif reason == "launch_held":
                    self.assertTrue(launch.startswith("no: launch hold"), launch)
                else:
                    self.assertEqual(launch, "yes, with an evidence warning")

    def test_readme_says_denied_only_for_a_recorded_denial(self):
        """ACC: N12 - a README status row uses the word "denied" only when the adapter's shell case recorded a non-zero denial counter; codex, claude and agy recorded none on 2026-11-19 (no command item, 0 permission_denials; agy: 0 tool errors and no denied_actions, y.txt absent), so their rows say what was observed instead."""
        for adapter in ("droid", "kiro", "codex", "claude", "agy", "vibe", "devin"):
            with self.subTest(adapter=adapter):
                _, doc = latest(adapter)
                row = readme_row(adapter)
                try:
                    observation = shell_case(doc).get("observation") or {}
                except StopIteration:
                    observation = {}
                recorded = sum(int(observation.get(k) or 0) for k in DENIAL_COUNTERS)
                if "denied" in row.lower():
                    self.assertGreater(recorded, 0, f"{adapter}: README says denied, results record no denial")
        _, codex = latest("codex")
        _, claude = latest("claude")
        self.assertEqual((shell_case(codex)["observation"]["commands"],
                          shell_case(codex)["observation"]["failed_commands"]), (0, 0))
        self.assertEqual(shell_case(claude)["observation"]["permission_denials"], 0)
        self.assertNotIn("denied", readme_row("codex").lower())
        self.assertNotIn("denied", readme_row("claude").lower())
        _, agy = latest("agy")
        agy_shell = shell_case(agy)["observation"]
        self.assertEqual((agy_shell["outcome"], agy_shell["tool_errors"], agy_shell["denied_actions"],
                          agy_shell["y_txt_exists"]), ("deny", 0, None, False))
        self.assertNotIn("denied", readme_row("agy").lower())
        self.assertIn("`touch y.txt` prompts left no file", readme_row("agy"))

    def test_smoke_tier_table_quotes_the_results_files(self):
        """ACC: N12 - the results table in docs/adapters.md ("Smoke tier for codex, claude and devin") gives each adapter's results file, recorded model and profile, and its launch-case and shell-case facts as recorded; the claude row and the README say the model is DeepSeek through a proxy."""
        text = read("docs/adapters.md")
        start = text.index("## Smoke tier for codex, claude and devin")
        section = text[start:text.index("\n## ", start + 1)]
        for adapter in SMOKE_TIER_ADAPTERS:
            with self.subTest(adapter=adapter):
                rel, doc = latest(adapter)
                row = table_row(section, rf"{adapter} {re.escape(doc['version'])}\b")
                cells = [c.strip() for c in row.strip().strip("|").split("|")]
                self.assertEqual(len(cells), 7, cells)
                self.assertEqual(cells[1], f"`{rel}`")
                self.assertIn(f"`{doc['model']}`", cells[2])
                self.assertEqual(cells[3], f"`{doc['profile']}`")
                self.assertEqual(doc["profile"], smoke_schema.smoke_profile(adapter))
                launch = [case(doc, cid)["status"] for cid in smoke_schema.LAUNCH_CASES]
                self.assertEqual(cells[4] == "S-X-1 and S-X-6 pass", launch == ["pass", "pass"])
                self.assertEqual(cells[6].startswith("enabled"), adapter not in evidence.LAUNCH_HOLDS)
                for cid in smoke_schema.UNSUPPORTED_CASES.get(adapter, ()):
                    self.assertEqual(case(doc, cid)["status"], "not_applicable", cid)
        _, claude = latest("claude")
        _, devin = latest("devin")
        self.assertEqual(claude["model"], "ds-flash")
        self.assertIn("DeepSeek", table_row(section, r"claude 2\.1\.325"))
        self.assertIn("DeepSeek", readme_row("claude"))
        self.assertIn("0 `permission_denials`", table_row(section, r"claude 2\.1\.325"))
        devin_shell = shell_case(devin)["observation"]
        self.assertEqual((devin_shell["outcome"], devin_shell["y_txt_exists"], devin_shell["profile"]),
                         ("executed", True, "unrestricted"))
        self.assertIn("ran `touch y.txt`", table_row(section, r"devin 3000"))
        self.assertIn("unrestricted", readme_row("devin"))


if __name__ == "__main__":
    unittest.main()
