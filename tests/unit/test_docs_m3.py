"""Documentation checks for gate M3-lite run m3-r7b (docs from results).

ACC: N9, N12, N27

Offline only: reads docs/adapters.md, docs/usage.md and the committed evidence under
tests/smoke/results/ and tests/fixtures/real/. Expected values are fixed literals taken
from the live `bin/mesh adapter show <id>` run recorded in docs/impl-notes-m3-r7b.md.
"""

import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STATUS_WORDS = {"verified", "partially_verified", "not_installed", "unsupported"}

# Live `adapter show` (2026-11-10, main adebc01): version, status, launch_enabled, launch_reason,
# results file, launch_milestone. "—" = null, "not run" = no results file.
EXPECTED_ROWS = {
    "droid": ("1.6.0", "verified", "true", "—", "tests/smoke/results/droid-1.6.0-2026-11-09-3.json", "—"),
    "agy": ("1.5.4", "partially_verified", "false", "no_smoke_evidence", "not run", "M3"),
    "kiro": ("3.1.0", "partially_verified", "true", "—", "tests/smoke/results/kiro-3.1.0-2026-11-10-2.json", "—"),
    "vibe": ("3.1.0", "partially_verified", "false", "no_smoke_evidence", "not run", "M3"),
    "hermes": ("—", "not_installed", "false", "no_smoke_evidence", "not run", "M4"),
    "openclaw": ("—", "not_installed", "false", "no_smoke_evidence", "not run", "M4"),
}

EXPECTED_DROID_CASES = {
    "S-X-1": "pass",
    "S-X-2": "pass",
    "S-X-3": "pass",
    "S-X-4": "pass",
    "S-X-5": "pass",
    "S-X-6": "pass",
    "S-X-7": "observed",
    "S-DROID-8": "observed",
}


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def normalize(text):
    return " ".join(text.split())


def cells(line):
    return [c.strip().strip("`") for c in line.strip().strip("|").split("|")]


def section(doc, heading_prefix):
    start = doc.index(heading_prefix)
    nxt = doc.find("\n## ", start + 1)
    return doc[start:] if nxt == -1 else doc[start:nxt]


def status_rows(doc):
    rows = {}
    table = section(doc, "## Status of the six vendor CLIs")
    for line in table.splitlines():
        if not line.startswith("|"):
            continue
        for adapter in EXPECTED_ROWS:
            if f"(`{adapter}`)" in line:
                rows[adapter] = cells(line)
    return rows


class TestDocsM3(unittest.TestCase):
    """adapters.md and usage.md match the committed M3 evidence."""

    def test_n9_n27_status_table_one_word_and_launch_columns(self):
        """ACC: N9, N27 - each of the six CLIs has exactly one status word, launch_enabled with reason, results file or "not run", and milestone, matching live adapter show."""
        rows = status_rows(read("docs/adapters.md"))
        self.assertEqual(set(rows), set(EXPECTED_ROWS))
        for adapter, (version, status, enabled, reason, results, milestone) in EXPECTED_ROWS.items():
            with self.subTest(adapter=adapter):
                row = rows[adapter]
                self.assertEqual(len(row), 8, row)
                self.assertEqual([c for c in row if c in STATUS_WORDS], [status])
                self.assertEqual(row[2], version)
                self.assertEqual(row[3], status)
                self.assertEqual(row[4], enabled)
                self.assertEqual(row[5], reason)
                self.assertEqual(row[6], results)
                expected_warn = {
                    "droid": "Allowed when installed and profile supported",
                    "kiro": "Allowed when installed and profile supported",
                    "agy": "Allowed with evidence warning and supported profile",
                    "vibe": "Allowed with evidence warning and supported profile",
                    "hermes": "Cannot launch: stub adapter",
                    "openclaw": "Cannot launch: stub adapter",
                }
                self.assertEqual(row[7], expected_warn[adapter])
                # Launch enabled exactly when there is no reason (independent reading).
                self.assertEqual(enabled == "true", reason == "—")

    def test_n12_results_files_exist_and_match_adapter_and_version(self):
        """ACC: N12 - every results file the status table names exists, is for that adapter, and its version equals the table's live version."""
        rows = status_rows(read("docs/adapters.md"))
        named = 0
        for adapter, row in rows.items():
            if row[6] == "not run":
                continue
            named += 1
            path = os.path.join(ROOT, row[6])
            self.assertTrue(os.path.isfile(path), path)
            self.assertTrue(os.path.isfile(path + ".meta.json"), path)
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
            self.assertEqual(doc["adapter"], adapter)
            self.assertEqual(doc["version"], row[2])
        self.assertEqual(named, 2)

    def test_n27_droid_case_rows_cite_results_and_real_fixtures(self):
        """ACC: N27, N12 - every droid case row has its status from the results file and names an existing real fixture with a sidecar; every smoke row names an existing results file."""
        doc = read("docs/adapters.md")
        droid = section(doc, "## Factory droid (`droid`)")
        results_rel = "tests/smoke/results/droid-1.6.0-2026-11-09-3.json"
        self.assertIn(results_rel, droid)
        with open(os.path.join(ROOT, results_rel), encoding="utf-8") as handle:
            results = json.load(handle)
        by_id = {c["id"]: c for c in results["cases"]}

        seen = {}
        for line in droid.splitlines():
            m = re.match(r"\s*\|\s*(S-[A-Z]+-\d+)\b", line)
            if not m:
                continue
            row = cells(line)
            case_id, status, fixture = m.group(1), row[1], row[2]
            seen[case_id] = status
            self.assertEqual(status, by_id[case_id]["status"], case_id)
            self.assertEqual(fixture, by_id[case_id]["fixture"], case_id)
            self.assertTrue(fixture.startswith("tests/fixtures/real/droid/1.6.0/"), fixture)
            self.assertTrue(os.path.isfile(os.path.join(ROOT, fixture)), fixture)
            self.assertTrue(os.path.isfile(os.path.join(ROOT, fixture + ".meta.json")), fixture)
        self.assertEqual(seen, EXPECTED_DROID_CASES)

        # Every results path the page mentions exists (smoke rows, e2e results).
        mentioned = set(re.findall(r"tests/smoke/results/[\w.\-]+\.json", doc))
        self.assertGreaterEqual(len(mentioned), 4)
        for rel in mentioned:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)
        # Every real fixture path the page mentions exists.
        for rel in set(re.findall(r"tests/fixtures/real/[\w.\-/]+\.(?:jsonl|json|txt)", doc)):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)

    def test_n27_capability_rows_and_e2e_results(self):
        """ACC: N27 - capability rows copy live verification words; N13 and N26 results are cited and passed."""
        doc = read("docs/adapters.md")
        expected = {
            "droid 1.6.0": ["yes / smoke"] * 4 + ["unknown / smoke"],
            "agy 1.5.4": ["yes / stale"] * 4 + ["unknown / stale"],
            "kiro 3.1.0": ["yes / smoke", "yes / smoke", "yes / help", "yes / help", "unknown / smoke"],
            "vibe 3.1.0": ["yes / help", "yes / help", "yes / unverified", "no / help", "unknown / help"],
            "hermes": ["unknown / none"] * 5,
            "openclaw": ["unknown / none"] * 5,
        }
        rows = {}
        for line in section(doc, "### Capability rows").splitlines():
            if line.startswith("|") and cells(line)[0] in expected:
                rows[cells(line)[0]] = cells(line)[1:]
        self.assertEqual(rows, expected)

        for case in ("N13", "N26"):
            names = re.findall(rf"tests/smoke/results/e2e-{case}-droid-1\.6\.0-[\w]+\.json", doc)
            self.assertEqual(len(set(names)), 1, case)
            with open(os.path.join(ROOT, names[0]), encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["status"], "pass")

    def test_n27_deferred_list(self):
        """ACC: N27 - the deferred list names kiro/agy/vibe smoke, kiro/agy dev, idle timeout, vendor lead turns, resume, hermes/openclaw and N11 scoped."""
        deferred = normalize(section(read("docs/adapters.md"), "## Deferred"))
        for phrase in (
            "kiro, agy and vibe smoke",
            "kiro and agy `dev`",
            "Idle timeout",
            "Vendor lead turns",
            "Resume.",
            "hermes and openclaw",
            "coverage is incomplete",
            "agy/vibe have no promoted results",
            "S-X-3, S-X-5, S-X-7 and S-KIRO-8 were not run",
        ):
            self.assertIn(phrase, deferred)

    def test_n9_no_false_verified_claims_and_test_double(self):
        """ACC: N9 - only droid is called verified; fake stays a test double, not vendor support."""
        doc = normalize(read("docs/adapters.md"))
        for adapter in ("agy", "kiro", "vibe"):
            self.assertNotRegex(doc, rf"\(`{adapter}`\)[^|]*\| yes \| [\d.]+ \| verified \|")
        self.assertIn("never counted as vendor support", doc)
        self.assertIn("not a vendor integration", doc)

    def test_n12_usage_probe_smoke_and_launch_rules(self):
        """ACC: N12 - usage.md documents probe/smoke consent and env vars, coordinator-only promotion, evidence-driven launch from tests/ (D5), D4 lead refusal, no sandbox, idle timeout 0, droid dev commits locally."""
        usage = normalize(read("docs/usage.md"))
        for phrase in (
            "Mesh does not sandbox external CLIs",
            "MESH_SMOKE=1",
            "MESH_SMOKE_ADAPTERS",
            "MESH_SMOKE_CONSENT",
            "MESH_SMOKE_MAX_RUNS",
            "`auto` is refused",
            "MESH_SMOKE_OUT",
            "MESH_PROBE=1",
            "Promotion is coordinator-only",
            "python3 -m tests.tierlib promote",
            "Stage it",
            "meshd reads this evidence from the checkout's `tests/` tree at runtime",
            "strict evidence checking",
            "`tests/smoke/results/`",
            "vendor_lead_m3_lite",
            "every vendor launch lead is refused, even with launch evidence",
            "`run.idle_timeout_s` defaults to `0`",
            "droid `dev` is the profile that commits locally",
            "Mesh never auto-commits",
            "Neither tier is part of `bash scripts/test.sh`",
        ):
            self.assertIn(phrase, usage)


    def test_m3_r7b2_negative_launch_and_resume_claims(self):
        """ACC: N27 - the page says no vendor run resumes today; usage section 1 names droid 1.6.0 and kiro as launchable (kiro N13 passed in M4, hold L11 lifted)."""
        adapters = normalize(read("docs/adapters.md"))
        self.assertIn("so no vendor run resumes today", adapters)
        usage = normalize(read("docs/usage.md"))
        self.assertIn("Today that is `droid` 1.6.0 and `kiro` (kiro-cli 3.1.0)", usage)
        self.assertNotIn("held until M4", usage)
        adapters_raw = read("docs/adapters.md")
        self.assertIn("e2e-N13-kiro-3.1.0-run_a1cfe019a5dgqkf4tu7kwjs7sf.json", adapters_raw)


if __name__ == "__main__":
    unittest.main()
